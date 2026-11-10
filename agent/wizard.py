"""Guided first-run setup for the DBSentinel agent.

Started with `--setup`, or automatically when the agent is opened (for example by double-clicking the exe)
and no agent.config.json exists yet. It asks for the server address and an enrollment code (or the shared
ingest key), optionally a local PostgreSQL, writes the config file, checks the connection and can register
the auto-start task.
"""
import json
import os
import socket
import subprocess
from pathlib import Path
from urllib.parse import quote, urlparse

import autostart
import tui

DEFAULT_API_URL = "https://dbsentinel.onrender.com"
INTRO = ("This agent reports the CPU, memory, disk and process activity of this computer, and optionally its "
         "PostgreSQL activity, to a DBSentinel server so it can be viewed on the dashboard.")
CODE_LENGTH = 12

ui = tui.Console()  # replaced for each run by run_setup()


def _on_windows():
    """Looked up at call time so tests can pretend to be on Windows without touching os.name."""
    return os.name == "nt"


# These two are looked up at call time so tests can replace them.
def _line(prompt):
    return input(prompt)


def _secret(prompt):
    return ui.read_secret(prompt)


def ask(label, default=None, secret=False, validate=None, hint=None):
    while True:
        prompt = ui.prompt_text(label, default=None if secret else default, hint=hint)
        value = (_secret if secret else _line)(prompt).strip()
        if not value and default is not None:
            value = default
        problem = validate(value) if validate else None
        if problem:
            ui.problem(problem)
            continue
        return value


def yes_no(label, default=True):
    while True:
        answer = _line(ui.yes_no_text(label, default)).strip().lower()
        if not answer:
            return default
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        ui.problem("Please answer y or n.")


def url_problem(url):
    """None when `url` is acceptable as the server address, else a short explanation."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return "The address must look like https://your-server.example.com"
    if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        return "Use https:// (plain http would send credentials unencrypted)."
    return None


def code_problem(text):
    """Empty is allowed (it means: use an ingest key). Otherwise the code must have 12 letters/digits."""
    letters = [ch for ch in text if ch.isalnum()]
    if not text or len(letters) == CODE_LENGTH:
        return None
    return "An enrollment code looks like K7P2-9XQM-4TWH (12 letters and digits)."


def build_dsn(host, port, user, password, database):
    """A PostgreSQL connection URL; user and password are percent-encoded so characters like @ : / are safe."""
    return f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}/{quote(database, safe='')}"


def lock_down(path):
    """Best effort: on Windows, remove other users' access to the file that holds the credentials."""
    if os.name != "nt" or not os.environ.get("USERNAME"):
        return
    try:
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{os.environ['USERNAME']}:F"],
                       capture_output=True, timeout=20)
    except Exception:
        pass


def offer_autostart(console=None):
    """Ask whether to start the agent at every Windows login and, if so, register it. Only for the packaged
    exe on Windows. Returns True when auto-start was set up."""
    import sys
    if not (getattr(sys, "frozen", False) and _on_windows()):
        return False
    return _ask_autostart(console)


def _ask_autostart(console=None):
    import sys
    global ui
    if console is not None:
        ui = console
    ui.write()
    if not yes_no("Start the agent automatically whenever you log in to Windows?", default=True):
        return False
    ok, message = autostart.install(Path(sys.executable))
    (ui.ok if ok else ui.warn)(message)
    return ok


def run_setup(config_path, send_test, probe_snapshot, frozen=False, console=None, version="", enroll=None):
    """Run the wizard. Returns "run" (start the agent in this window), "autostart" (task installed and started),
    "done" (config saved, nothing started) or "cancel" (nothing changed).

    send_test(config_path) -> (ok, message) sends one snapshot; probe_snapshot(dsn) -> the PostgreSQL probe result;
    enroll(api_url, code) -> (ok, message, token, label) trades an enrollment code for a device token. When
    `enroll` is None the wizard only offers the shared ingest key."""
    global ui
    ui = console or tui.Console()
    config_path = Path(config_path)

    ui.header("Agent setup", f"v{version}" if version else "")
    ui.write()
    ui.paragraph(INTRO)
    ui.paragraph("Press Enter to accept a value shown in [brackets].")

    existing = {}
    if config_path.exists():
        ui.write()
        if not yes_no(f"{config_path.name} already exists. Replace it?", default=False):
            ui.note("Keeping the existing configuration.")
            return "cancel"
        try:
            existing = json.loads(config_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            existing = {}

    # ---------------------------------------------------------------- 1. server and access
    ui.section(1, "Server")
    api_url = ask("Server address", default=existing.get("api_url") or DEFAULT_API_URL, validate=url_problem).rstrip("/")
    credentials, label = {}, ""
    if enroll is not None:
        while not credentials:
            code = ask("Enrollment code", default="", validate=code_problem, hint="from the administrator; Enter to use an ingest key")
            if not code:
                break
            with ui.spinner("Enrolling this computer") as spin:
                ok, message, token, label = enroll(api_url, code)
                spin.finish(ok, f"Enrolled{' as ' + label if label else ''}" if ok else f"Enrollment failed: {message}")
            if ok:
                credentials = {"device_token": token}
    if not credentials:
        credentials = {"api_key": ask("Ingest key", secret=True, hint="input hidden", validate=lambda v: None if v else "The key cannot be empty.")}

    # ---------------------------------------------------------------- 2. this computer
    ui.section(2, "This computer")
    device_name = ask("Display name", default=existing.get("device_name") or label or socket.gethostname())

    # ---------------------------------------------------------------- 3. postgresql
    ui.section(3, "PostgreSQL")
    postgres = {"enabled": False, "dsn": "", "send_query_text": True}
    if yes_no("Monitor a PostgreSQL server running on this computer?", default=False):
        ui.write()
        host = ask("Host", default="localhost")
        port = ask("Port", default="5432", validate=lambda v: None if v.isdigit() else "The port must be a number.")
        user = ask("User", default="postgres")
        password = ask("Password", secret=True, hint="input hidden")
        database = ask("Database", default="postgres")
        dsn = build_dsn(host, port, user, password, database)
        ui.write()
        with ui.spinner(f"Connecting to PostgreSQL at {host}:{port}") as spin:
            result = probe_snapshot(dsn)
            spin.finish(result["connected"], result["message"] if result["connected"] else f"Could not connect: {result['message']}")
        if result["connected"] or yes_no("Save these PostgreSQL settings anyway?", default=False):
            postgres.update(enabled=True, dsn=dsn)
            ui.write()
            postgres["send_query_text"] = yes_no("Send the SQL text of queries to the dashboard?", default=True)
            ui.note("Query text can contain sensitive values.")
    else:
        ui.note("Only operating-system data will be reported.")

    config = {"api_url": api_url, **credentials, "device_name": device_name, "interval_seconds": 5, "postgres": postgres}
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    lock_down(config_path)

    # ---------------------------------------------------------------- 4. connection check
    ui.section(4, "Connection check")
    ui.note("The first request can take up to a minute if the server was idle.")
    with ui.spinner(f"Contacting {api_url}") as spin:
        ok, message = send_test(config_path)
        spin.finish(ok, f"The server accepted a snapshot ({message})" if ok else f"The server did not accept the snapshot: {message}")
    ui.note("Settings saved to " + ui.tail(str(config_path), ui.width - 20))
    if not ok:
        ui.write()
        ui.note("Check the code or key and the address, then open the agent again or run it with --setup.")
        return "done"

    # ---------------------------------------------------------------- 5. startup
    started_at_login = False
    if frozen and _on_windows():
        ui.section(5, "Startup")
        started_at_login = _ask_autostart(ui)

    ui.panel("Summary", [
        ("Server", api_url),
        ("Device", device_name),
        ("Access", "own device token" if "device_token" in credentials else "shared ingest key"),
        ("PostgreSQL", "monitored" if postgres["enabled"] else "not monitored"),
        ("Auto-start", "enabled at login" if started_at_login else "off"),
    ])
    ui.write()
    ui.note("Open the DBSentinel dashboard and choose this computer in the device list.")
    if frozen:
        ui.note("To change settings or remove the agent later, open dbsentinel-agent.exe again and choose from its menu.")
    if started_at_login:
        return "autostart"
    ui.write()
    return "run" if yes_no("Run the agent now, in this window?", default=True) else "done"
