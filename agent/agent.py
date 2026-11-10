"""DBSentinel agent: collects this machine's OS + local PostgreSQL telemetry and pushes it to the API.

    python agent.py                 run forever (Ctrl+C to stop)
    python agent.py --dry-run       print one snapshot as JSON and exit (nothing is sent)
    python agent.py --once          send one snapshot and exit 0/1 (connectivity + key check)
    python agent.py --config FILE   use a specific config file
    python agent.py --background    no console window, log to agent.log only (used by the auto-start task)
    python agent.py --setup         guided setup: asks a few questions and writes agent.config.json
    python agent.py --install-autostart   start the agent at every Windows login (packaged exe)
    python agent.py --uninstall     stop the agent, remove auto-start and (optionally) its saved settings

Credentials (config file or DBSENTINEL_* variables): enroll_code (one-time code from the dashboard, traded for a
device_token on first start), device_token, or the shared api_key.
"""
import argparse
import json
import logging
import logging.handlers
import os
import socket
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlparse

import psutil

from collector import DatabaseProbe, OSCollector
import tui
import autostart
from wizard import lock_down, offer_autostart, run_setup

AGENT_VERSION = "0.4.0"
log = logging.getLogger("dbsentinel-agent")


class ConfigError(Exception):
    """The agent cannot start because its configuration is missing or wrong."""

HEARTBEAT_EVERY = 60  # successful sends between "still running" log lines (about 5 minutes)


def app_dir():
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


def load_config(path):
    cfg = {}
    if path.exists():
        try:
            cfg = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise ConfigError(f"Cannot read config file {path}: {exc}")
    pg = cfg.get("postgres") or {}
    env = os.environ
    cfg["api_url"] = (env.get("DBSENTINEL_API_URL") or cfg.get("api_url") or "").rstrip("/")
    cfg["api_key"] = env.get("DBSENTINEL_API_KEY") or cfg.get("api_key") or ""
    cfg["device_token"] = env.get("DBSENTINEL_DEVICE_TOKEN") or cfg.get("device_token") or ""
    cfg["enroll_code"] = env.get("DBSENTINEL_ENROLL_CODE") or cfg.get("enroll_code") or ""
    cfg["name_configured"] = bool(cfg.get("device_name"))
    cfg["device_name"] = cfg.get("device_name") or socket.gethostname()
    cfg["interval_seconds"] = max(2.0, float(cfg.get("interval_seconds", 5)))
    cfg["request_timeout_seconds"] = float(cfg.get("request_timeout_seconds", 30))
    cfg["allow_insecure_http"] = bool(cfg.get("allow_insecure_http", False))
    cfg["postgres"] = {"dsn": env.get("DBSENTINEL_PG_DSN") or pg.get("dsn") or "",
                       "enabled": bool(pg.get("enabled", True)),
                       "send_query_text": bool(pg.get("send_query_text", True))}
    return cfg


def validate_config(cfg, path):
    problems = []
    if not path.exists():
        problems.append(f"config file not found: {path} (copy agent.config.example.json to agent.config.json next to the agent and edit it)")
    if not cfg["api_url"]:
        problems.append("api_url is missing (config file or DBSENTINEL_API_URL)")
    else:
        parsed = urlparse(cfg["api_url"])
        local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            problems.append("api_url must look like https://host")
        elif parsed.scheme == "http" and not local and not cfg["allow_insecure_http"]:
            problems.append("api_url uses plain http to a remote host, which would send credentials unencrypted; "
                            "use https (or set allow_insecure_http=true if you really mean it)")
    if not (cfg["api_key"] or cfg["device_token"] or cfg["enroll_code"]):
        problems.append("no credential is configured: set enroll_code (a one-time code from the dashboard), device_token or api_key "
                        "(config file or DBSENTINEL_ENROLL_CODE / DBSENTINEL_DEVICE_TOKEN / DBSENTINEL_API_KEY)")
    if problems:
        raise ConfigError("Configuration problem(s):\n  - " + "\n  - ".join(problems))


def load_device_id(state_path):
    try:
        device_id = json.loads(state_path.read_text(encoding="utf-8")).get("device_id")
        if isinstance(device_id, str) and 8 <= len(device_id) <= 64:
            return device_id
    except (OSError, ValueError, AttributeError):
        pass
    device_id = "dev-" + uuid.uuid4().hex[:24]
    try:
        state_path.write_text(json.dumps({"device_id": device_id}), encoding="utf-8")
    except OSError as exc:
        log.warning("Could not save %s (%s); this run uses a temporary device id.", state_path, exc)
    return device_id


def build_payload(cfg, device_id, collector, probe):
    snap = collector.snapshot()
    return {"device_id": device_id, "device_name": cfg["device_name"], "agent_version": AGENT_VERSION,
            "os": snap["os"], "partitions": snap["partitions"], "processes": snap["processes"],
            "database": probe.snapshot()}


def error_detail(exc):
    """The `detail` message of an HTTP error response from the API (or its raw text)."""
    raw = exc.read(400).decode("utf-8", "replace").strip()
    try:
        detail = json.loads(raw).get("detail")
        if isinstance(detail, str) and detail:
            return detail
    except (ValueError, AttributeError):
        pass
    return raw


def auth_headers(cfg):
    """A device with its own token authenticates with it; otherwise the shared ingest key is used."""
    if cfg.get("device_token"):
        return {"X-Device-Token": cfg["device_token"]}
    return {"X-API-Key": cfg.get("api_key", "")}


def hint_for(cfg, status, detail):
    text = detail.lower()
    if status == 401 and "revoked" in text:
        return " - this device's access was revoked; ask the administrator for a new enrollment code, then run the agent with --setup"
    if status == 401 and cfg.get("device_token"):
        return " - the device token was not accepted; run the agent with --setup to enroll again"
    if status == 401:
        return " - the API key was rejected; check api_key against INGEST_KEY on the server"
    if status == 403 and "different device" in text:
        return " - the token belongs to another device (agent.state.json was deleted or copied); run the agent with --setup"
    if status == 403 and "disabled" in text:
        return " - the server no longer accepts the shared key; enroll with a code (run the agent with --setup)"
    return ""


def send(cfg, payload):
    """POST one snapshot. Returns (ok, message)."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        cfg["api_url"] + "/api/ingest", data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": f"dbsentinel-agent/{AGENT_VERSION}", **auth_headers(cfg)})
    try:
        with urllib.request.urlopen(req, timeout=cfg["request_timeout_seconds"]) as resp:
            return True, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        detail = error_detail(exc)
        return False, f"HTTP {exc.code}{hint_for(cfg, exc.code, detail)}: {detail}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, f"cannot reach {cfg['api_url']}: {getattr(exc, 'reason', exc)}"


def enroll(api_url, code, device_id, device_name="", timeout=30):
    """Exchange a one-time enrollment code for this device's own token. Returns (ok, message, token, label)."""
    body = json.dumps({"code": code, "device_id": device_id, "device_name": device_name}).encode("utf-8")
    req = urllib.request.Request(api_url + "/api/enroll", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": f"dbsentinel-agent/{AGENT_VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        return True, "enrolled", data["token"], data.get("label", "")
    except urllib.error.HTTPError as exc:
        return False, error_detail(exc), "", ""
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, f"cannot reach {api_url}: {getattr(exc, 'reason', exc)}", "", ""
    except (ValueError, KeyError):
        return False, "the server sent an unexpected answer", "", ""


def update_config_file(path, set_values=None, remove=()):
    """Change a few keys of the config file in place (creating it if needed) and keep it private."""
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    except (OSError, ValueError):
        data = {}
    data.update(set_values or {})
    for key in remove:
        data.pop(key, None)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    lock_down(path)


def ensure_enrolled(cfg, config_path, device_id):
    """If the config holds an enrollment code, trade it for a device token and store the token instead.

    Returns (ok, message). The code is single-use, so it is removed from the file as soon as it worked."""
    if cfg["device_token"] or not cfg["enroll_code"]:
        return True, ""
    ok, message, token, label = enroll(cfg["api_url"], cfg["enroll_code"], device_id, cfg["device_name"], cfg["request_timeout_seconds"])
    if not ok:
        return False, message
    cfg["device_token"], cfg["enroll_code"] = token, ""
    changes = {"device_token": token, "api_url": cfg["api_url"]}
    if label and not cfg["name_configured"]:
        cfg["device_name"] = label
        changes["device_name"] = label
    try:
        update_config_file(config_path, changes, remove=("enroll_code",))
    except OSError as exc:
        log.warning("Enrolled, but could not save the token to %s (%s). It works for this run only.", config_path, exc)
    return True, f"enrolled as {cfg['device_name']}"


def launcher_process():
    """A PyInstaller one-file exe is a tiny launcher process that starts the real agent as its child.
    If only the launcher is killed, the child would live on as an orphan, so we watch for that."""
    if not getattr(sys, "frozen", False):
        return None
    try:
        return psutil.Process().parent()
    except Exception:
        return None


def run(cfg, device_id, show_banner=False):
    launcher = launcher_process()
    collector = OSCollector()
    probe = DatabaseProbe(cfg["postgres"]["enabled"], cfg["postgres"]["dsn"], cfg["postgres"]["send_query_text"])
    time.sleep(1.0)  # let the primed CPU counters accumulate a real first interval
    interval, fails, sent = cfg["interval_seconds"], 0, 0
    if show_banner:
        screen = tui.Console()
        screen.header("Agent", f"v{AGENT_VERSION}")
        screen.write()
        screen.kv([("Device", f"{cfg['device_name']}  ({device_id})"), ("Server", cfg["api_url"]),
                   ("Interval", f"every {interval:g} s"), ("PostgreSQL", "monitored" if probe.enabled else "not monitored")])
        screen.write()
        screen.note("Press Ctrl+C to stop.")
        screen.write()
    log.info("Agent %s started: device %s (%s) -> %s every %gs; PostgreSQL monitoring %s",
             AGENT_VERSION, device_id, cfg["device_name"], cfg["api_url"], interval,
             "on" if probe.enabled else "off")
    try:
        while True:
            if launcher is not None and not launcher.is_running():
                log.warning("The launcher process has exited; stopping so no orphan agent is left behind.")
                return
            started = time.monotonic()
            try:
                ok, message = send(cfg, build_payload(cfg, device_id, collector, probe))
            except Exception:  # a collection bug must never kill the agent
                log.exception("Snapshot failed")
                ok, message = False, "snapshot error (see above)"
            if ok:
                if fails:
                    log.info("Connection to the API restored after %d failed attempt(s).", fails)
                fails, sent = 0, sent + 1
                if sent == 1 or sent % HEARTBEAT_EVERY == 0:
                    log.info("Sending snapshots normally (%d sent this run).", sent)
            else:
                fails += 1
                log.warning("Send failed (attempt %d): %s", fails, message)
            # back off while the API is down or waking up (Render free tier), but keep collecting fresh data
            delay = interval if ok else min(interval * (2 ** min(fails, 4)), 60.0)
            time.sleep(max(0.5, delay - (time.monotonic() - started)))
    except KeyboardInterrupt:
        log.info("Stopped.")
    finally:
        probe.close()


def setup_logging(verbose, console=True, to_file=True):
    level = logging.DEBUG if verbose else logging.INFO
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handlers = []
    stream = None
    if console and sys.stderr is not None:
        stream = logging.StreamHandler(sys.stdout if sys.stdout is not None else sys.stderr)
        handlers.append(stream)
    try:
        if to_file:
            handlers.append(logging.handlers.RotatingFileHandler(app_dir() / "agent.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8"))
    except OSError:
        pass
    for h in handlers:
        h.setFormatter(tui.ConsoleFormatter(tui.Console(stream=h.stream)) if h is stream else fmt)
        log.addHandler(h)
    log.setLevel(level)


def detach_console():
    """Close this process's console window (Windows). Logging continues in agent.log."""
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.FreeConsole()
        except Exception:
            pass


_lock_handle = None  # kept open (and locked) for the life of the process


def acquire_single_instance(path):
    """Take an exclusive lock so two agents never push for the same device. False if one is already running."""
    global _lock_handle
    try:
        handle = open(path, "a+")
    except OSError as exc:
        log.warning("Could not create %s (%s); not enforcing a single instance.", path, exc)
        return True
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    _lock_handle = handle
    return True


def make_probe(cfg):
    pg = cfg["postgres"]
    return DatabaseProbe(pg["enabled"], pg["dsn"], pg["send_query_text"])


def launched_from_explorer():
    """True when this process owns its console window alone (double-clicked), so the window would vanish on exit."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        slots = (ctypes.c_uint * 8)()
        count = ctypes.windll.kernel32.GetConsoleProcessList(slots, 8)
        # a one-file exe is a launcher plus the real agent (2 processes); from source it is one
        return count <= (2 if getattr(sys, "frozen", False) else 1)
    except Exception:
        return False


def pause_before_exit():
    if launched_from_explorer() and tui.stdin_is_console():
        try:
            input("\nPress Enter to close this window...")
        except EOFError:
            pass


def cmd_install_autostart():
    screen = tui.Console()
    if not getattr(sys, "frozen", False):
        screen.fail("Auto-start can only be set up from the packaged dbsentinel-agent.exe.")
        return 1
    ok, message = autostart.install(Path(sys.executable))
    (screen.ok if ok else screen.fail)(message)
    return 0 if ok else 1


def cmd_uninstall(assume_yes):
    """Stop the agent, remove auto-start and, if wanted, the settings it saved next to the exe."""
    screen = tui.Console()
    screen.header("Agent uninstall", f"v{AGENT_VERSION}")
    screen.write()
    frozen = getattr(sys, "frozen", False)
    ok, message = autostart.uninstall()
    (screen.ok if ok else screen.fail)(message)
    if frozen:
        stopped = autostart.stop_running_agents(Path(sys.executable))
        screen.ok(f"Stopped {stopped} running agent{'s' if stopped != 1 else ''}." if stopped else "No agent was running.")
    files = autostart.settings_files(app_dir())
    if files:
        screen.write()
        screen.note("Saved by the agent in this folder: " + ", ".join(f.name for f in files))
        delete = assume_yes
        if not assume_yes:
            try:
                answer = input(screen.yes_no_text("Delete these saved settings and logs too?", True)).strip().lower()
                delete = answer in ("", "y", "yes")
            except EOFError:
                delete = False
        if delete:
            deleted, failed = autostart.delete_settings(app_dir())
            if deleted:
                screen.ok("Deleted " + ", ".join(deleted))
            if failed:
                screen.warn("Could not delete " + ", ".join(failed) + " (is another agent still running?)")
    screen.write()
    screen.note("Done. You can now delete dbsentinel-agent.exe. The administrator can also revoke this computer in the dashboard.")
    return 0 if ok else 1


MENU_ITEMS = ("Run the agent in this window", "{autostart}", "Change settings (run the setup again)",
              "Uninstall (stop the agent and remove it)", "Quit")


def show_menu(args):
    """What opening an already set-up dbsentinel-agent.exe shows: status, then a few choices.
    Returns "run" when the user wants the agent started in this window, otherwise "quit"."""
    screen = tui.Console()
    exe = Path(sys.executable)
    while True:
        installed = autostart.is_installed()
        running = autostart.count_running_agents(exe)
        try:
            cfg = load_config(args.config)
            device, server = cfg["device_name"], cfg["api_url"]
        except ConfigError:
            device = server = "(not configured)"
        screen.header("Agent", f"v{AGENT_VERSION}")
        screen.write()
        screen.kv([("Device", device), ("Server", server), ("Auto-start", "on" if installed else "off"),
                   ("Running now", "yes, in the background" if running else "no")])
        screen.write()
        for number, text in enumerate(MENU_ITEMS, 1):
            label = text.format(autostart="Turn auto-start off" if installed else "Turn auto-start on")
            screen.line(screen.paint(f"{number}", "accent", bold=True) + "  " + label)
        screen.write()
        try:
            choice = input(screen.prompt_text("Choose", hint="Enter to quit")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return "quit"
        if choice in ("", "5", "q", "quit"):
            return "quit"
        if choice == "1":
            if running:
                screen.note("The agent is already running in the background, so there is nothing to start.")
                continue
            return "run"
        if choice == "2":
            ok, message = autostart.uninstall() if installed else autostart.install(exe)
            (screen.ok if ok else screen.fail)(message)
        elif choice == "3":
            try:
                if run_wizard(args) == "run":
                    return "run"
            except (EOFError, KeyboardInterrupt):
                screen.note("Setup cancelled.")
        elif choice == "4":
            cmd_uninstall(False)
            return "quit"
        else:
            screen.problem("Please choose a number from 1 to 5.")


def run_wizard(args):
    def send_test(config_path):
        cfg = load_config(config_path)
        collector = OSCollector()
        time.sleep(1.0)
        probe = make_probe(cfg)
        try:
            return send(cfg, build_payload(cfg, load_device_id(app_dir() / "agent.state.json"), collector, probe))
        finally:
            probe.close()

    def probe_snapshot(dsn):
        probe = DatabaseProbe(True, dsn)
        try:
            return probe.snapshot()
        finally:
            probe.close()

    def enroll_with_code(api_url, code):
        return enroll(api_url, code, load_device_id(app_dir() / "agent.state.json"))

    return run_setup(args.config, send_test, probe_snapshot, frozen=getattr(sys, "frozen", False),
                     version=AGENT_VERSION, enroll=enroll_with_code)


def enroll_on_start(cfg, args, device_id, announce):
    """First start with a pre-filled package: trade the enrollment code for a token. Returns True when ready to send."""
    if cfg["device_token"] or not cfg["enroll_code"]:
        return True
    screen = tui.Console() if announce else None
    if screen:
        screen.header("Agent", f"v{AGENT_VERSION}")
        screen.write()
        with screen.spinner("Enrolling this computer") as spin:
            ok, message = ensure_enrolled(cfg, args.config, device_id)
            spin.finish(ok, f"Enrolled as {cfg['device_name']}" if ok else f"Enrollment failed: {message}")
        if not ok:
            screen.note("Ask the administrator for a new enrollment code and run the agent with --setup.")
        return ok
    ok, message = ensure_enrolled(cfg, args.config, device_id)
    (log.info if ok else log.error)("Enrollment: %s", message)
    return ok


def execute(args):
    interactive = tui.stdin_is_console()
    plain_start = not (args.once or args.dry_run or args.background or args.setup)
    # An already set-up packaged exe opened by a person shows a menu instead of silently running.
    if (getattr(sys, "frozen", False) and interactive and plain_start and args.config.exists()
            and (app_dir() / "agent.state.json").exists()):
        if show_menu(args) != "run":
            return 0
    first_run = not args.config.exists() and interactive and not (args.once or args.dry_run or args.background)
    if args.setup or first_run:
        try:
            outcome = run_wizard(args)
        except (EOFError, KeyboardInterrupt):
            print("\nSetup cancelled.")
            return 1
        if outcome != "run":
            pause_before_exit()
            return 0
    cfg = load_config(args.config)
    state_path = app_dir() / "agent.state.json"
    first_launch = not state_path.exists()
    device_id = load_device_id(state_path)

    if args.dry_run:
        collector = OSCollector()
        time.sleep(1.0)
        probe = make_probe(cfg)
        print(json.dumps(build_payload(cfg, device_id, collector, probe), indent=2, default=str))
        probe.close()
        return 0
    validate_config(cfg, args.config)
    announce = interactive and not (args.once or args.background)
    if not enroll_on_start(cfg, args, device_id, announce):
        if announce:
            pause_before_exit()
        return 4
    if announce and first_launch and getattr(sys, "frozen", False):
        try:
            offer_autostart(tui.Console())               # a ready-made configuration skips the wizard, so ask here
        except (EOFError, KeyboardInterrupt):
            pass                                           # nobody is there to answer: carry on without auto-start
    if args.once:
        collector = OSCollector()
        time.sleep(1.0)
        probe = make_probe(cfg)
        ok, message = send(cfg, build_payload(cfg, device_id, collector, probe))
        probe.close()
        print(("Accepted by the API: " if ok else "FAILED: ") + message)
        return 0 if ok else 1
    if not acquire_single_instance(app_dir() / "agent.lock"):
        log.error("Another DBSentinel agent is already running from %s; exiting.", app_dir())
        return 3
    run(cfg, device_id, show_banner=not args.background)
    return 0


def main():
    parser = argparse.ArgumentParser(description="DBSentinel agent")
    parser.add_argument("--config", type=Path, default=app_dir() / "agent.config.json")
    parser.add_argument("--dry-run", action="store_true", help="print one snapshot as JSON and exit without sending")
    parser.add_argument("--once", action="store_true", help="send one snapshot and exit (0 = accepted)")
    parser.add_argument("--background", action="store_true",
                        help="no console window; log to agent.log only (used by the auto-start task)")
    parser.add_argument("--setup", action="store_true", help="guided setup: asks a few questions and writes the config file")
    parser.add_argument("--install-autostart", action="store_true", help="start the agent at every Windows login (packaged exe)")
    parser.add_argument("--uninstall", action="store_true", help="stop the agent, remove auto-start and optionally its saved settings")
    parser.add_argument("--yes", action="store_true", help="with --uninstall: do not ask, delete the saved settings too")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    setup_logging(args.verbose, console=not args.background, to_file=not args.uninstall)
    if args.background:
        detach_console()
    try:
        if args.install_autostart:
            return cmd_install_autostart()
        if args.uninstall:
            code = cmd_uninstall(args.yes)
            pause_before_exit()
            return code
        return execute(args)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
