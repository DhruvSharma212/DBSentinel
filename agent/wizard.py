"""Guided first-run setup for the DBSentinel agent.

Started with `--setup`, or automatically when the agent is opened (for example by double-clicking the exe)
and no agent.config.json exists yet. It asks for the server address and key, optionally a local PostgreSQL,
writes the config file, checks the connection and can register the auto-start task.
"""
import json
import os
import socket
import subprocess
from pathlib import Path
from urllib.parse import quote, urlparse

import tui

DEFAULT_API_URL = "https://dbsentinel.onrender.com"
INTRO = ("This agent reports the CPU, memory, disk and process activity of this computer, and optionally its "
         "PostgreSQL activity, to a DBSentinel server so it can be viewed on the dashboard.")

ui = tui.Console()  # replaced for each run by run_setup()


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


def run_setup(config_path, send_test, probe_snapshot, script_dir, frozen=False, console=None, version=""):
    """Run the wizard. Returns "run" (start the agent in this window), "autostart" (task installed and started),
    "done" (config saved, nothing started) or "cancel" (nothing changed).

    send_test(config_path) -> (ok, message) sends one snapshot; probe_snapshot(dsn) -> the PostgreSQL probe result."""
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

    # ---------------------------------------------------------------- 1. server
    ui.section(1, "Server")
    api_url = ask("Server address", default=existing.get("api_url") or DEFAULT_API_URL, validate=url_problem).rstrip("/")
    api_key = ask("Ingest key", secret=True, hint="input hidden", validate=lambda v: None if v else "The key cannot be empty.")

    # ---------------------------------------------------------------- 2. this computer
    ui.section(2, "This computer")
    device_name = ask("Display name", default=existing.get("device_name") or socket.gethostname())

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

    config = {"api_url": api_url, "api_key": api_key, "device_name": device_name, "interval_seconds": 5, "postgres": postgres}
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
        ui.note("Check the key and address, then open the agent again or run it with --setup.")
        return "done"

    # ---------------------------------------------------------------- 5. startup
    autostart = False
    script = Path(script_dir) / "install-autostart.ps1"
    if frozen and os.name == "nt" and script.exists():
        ui.section(5, "Startup")
        if yes_no("Start the agent automatically whenever you log in to Windows?", default=True):
            done = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                                  capture_output=True, text=True)
            autostart = done.returncode == 0
            (ui.ok if autostart else ui.warn)((done.stdout or done.stderr).strip().splitlines()[-1] if (done.stdout or done.stderr).strip() else "Auto-start finished")

    ui.panel("Summary", [
        ("Server", api_url),
        ("Device", device_name),
        ("PostgreSQL", "monitored" if postgres["enabled"] else "not monitored"),
        ("Auto-start", "enabled at login" if autostart else "off"),
    ])
    ui.write()
    ui.note("Open the DBSentinel dashboard and choose this computer in the device list.")
    if autostart:
        return "autostart"
    ui.write()
    return "run" if yes_no("Run the agent now, in this window?", default=True) else "done"
