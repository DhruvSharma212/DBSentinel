"""DBSentinel agent: collects this machine's OS + local PostgreSQL telemetry and pushes it to the API.

    python agent.py                 run forever (Ctrl+C to stop)
    python agent.py --dry-run       print one snapshot as JSON and exit (nothing is sent)
    python agent.py --once          send one snapshot and exit 0/1 (connectivity + key check)
    python agent.py --config FILE   use a specific config file
    python agent.py --background    no console window, log to agent.log only (used by the auto-start task)
    python agent.py --setup         guided setup: asks a few questions and writes agent.config.json
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
from wizard import run_setup

AGENT_VERSION = "0.2.0"
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
            problems.append("api_url uses plain http to a remote host, which would send the API key unencrypted; "
                            "use https (or set allow_insecure_http=true if you really mean it)")
    if not cfg["api_key"]:
        problems.append("api_key is missing (config file or DBSENTINEL_API_KEY)")
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


def send(cfg, payload):
    """POST one snapshot. Returns (ok, message)."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        cfg["api_url"] + "/api/ingest", data=body, method="POST",
        headers={"Content-Type": "application/json", "X-API-Key": cfg["api_key"],
                 "User-Agent": f"dbsentinel-agent/{AGENT_VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=cfg["request_timeout_seconds"]) as resp:
            return True, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        detail = exc.read(300).decode("utf-8", "replace").strip()
        hint = " - the API key was rejected; check api_key against INGEST_KEY on the server" if exc.code == 401 else ""
        return False, f"HTTP {exc.code}{hint}: {detail}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, f"cannot reach {cfg['api_url']}: {getattr(exc, 'reason', exc)}"


def launcher_process():
    """A PyInstaller one-file exe is a tiny launcher process that starts the real agent as its child.
    If only the launcher is killed, the child would live on as an orphan, so we watch for that."""
    if not getattr(sys, "frozen", False):
        return None
    try:
        return psutil.Process().parent()
    except Exception:
        return None


def run(cfg, device_id):
    launcher = launcher_process()
    collector = OSCollector()
    probe = DatabaseProbe(cfg["postgres"]["enabled"], cfg["postgres"]["dsn"], cfg["postgres"]["send_query_text"])
    time.sleep(1.0)  # let the primed CPU counters accumulate a real first interval
    interval, fails, sent = cfg["interval_seconds"], 0, 0
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


def setup_logging(verbose, console=True):
    level = logging.DEBUG if verbose else logging.INFO
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handlers = []
    if console and sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    try:
        handlers.append(logging.handlers.RotatingFileHandler(app_dir() / "agent.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8"))
    except OSError:
        pass
    for h in handlers:
        h.setFormatter(fmt)
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
    if launched_from_explorer() and sys.stdin is not None and sys.stdin.isatty():
        try:
            input("\nPress Enter to close this window...")
        except EOFError:
            pass


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

    return run_setup(args.config, send_test, probe_snapshot, app_dir(), frozen=getattr(sys, "frozen", False))


def execute(args):
    interactive = sys.stdin is not None and sys.stdin.isatty()
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
    device_id = load_device_id(app_dir() / "agent.state.json")

    if args.dry_run:
        collector = OSCollector()
        time.sleep(1.0)
        probe = make_probe(cfg)
        print(json.dumps(build_payload(cfg, device_id, collector, probe), indent=2, default=str))
        probe.close()
        return 0
    validate_config(cfg, args.config)
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
    run(cfg, device_id)
    return 0


def main():
    parser = argparse.ArgumentParser(description="DBSentinel agent")
    parser.add_argument("--config", type=Path, default=app_dir() / "agent.config.json")
    parser.add_argument("--dry-run", action="store_true", help="print one snapshot as JSON and exit without sending")
    parser.add_argument("--once", action="store_true", help="send one snapshot and exit (0 = accepted)")
    parser.add_argument("--background", action="store_true",
                        help="no console window; log to agent.log only (used by the auto-start task)")
    parser.add_argument("--setup", action="store_true", help="guided setup: asks a few questions and writes the config file")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    setup_logging(args.verbose, console=not args.background)
    if args.background:
        detach_console()
    try:
        return execute(args)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2


if __name__ == "__main__":
    sys.exit(main())
