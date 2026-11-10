"""Start the agent at every Windows login, and remove it again. Built into the exe, so no extra files are needed.

The agent registers a Scheduled Task for the current user that runs `dbsentinel-agent.exe --background` at logon.
It needs no administrator rights. `uninstall()` removes the task; `stop_running_agents()` ends copies that are
running right now; `delete_settings()` removes the files the agent wrote next to the exe.
"""
import base64
import os
import subprocess
import sys
from pathlib import Path

import psutil

DEFAULT_TASK = "DBSentinel Agent"
SETTINGS_FILES = ("agent.config.json", "agent.state.json", "agent.lock")


def task_name():
    """The scheduled task's name. The environment override exists so tests never touch a real installation."""
    return os.environ.get("DBSENTINEL_TASK_NAME") or DEFAULT_TASK


def _quote(text):
    return str(text).replace("'", "''")          # PowerShell single-quoted string


def _powershell(script, timeout=90):
    """Run a PowerShell script. It is passed encoded so paths with spaces or quotes cannot break the command line."""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
                          capture_output=True, text=True, timeout=timeout)


def install_script(exe_path, name=None):
    exe = Path(exe_path)
    return f"""$ErrorActionPreference = 'Stop'
$name = '{_quote(name or task_name())}'
$user = "$env:USERDOMAIN\\$env:USERNAME"
$action = New-ScheduledTaskAction -Execute '{_quote(exe)}' -Argument '--background' -WorkingDirectory '{_quote(exe.parent)}'
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'Reports this computer to a DBSentinel server.' -Force | Out-Null
Start-ScheduledTask -TaskName $name
"""


def uninstall_script(name=None):
    return f"""$name = '{_quote(name or task_name())}'
if (-not (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue)) {{ Write-Output 'NOTFOUND'; exit 0 }}
Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName $name -Confirm:$false
Write-Output 'REMOVED'
"""


def is_installed():
    if os.name != "nt":
        return False
    try:
        done = _powershell(f"if (Get-ScheduledTask -TaskName '{_quote(task_name())}' -ErrorAction SilentlyContinue) {{ 'YES' }} else {{ 'NO' }}")
        return "YES" in done.stdout
    except (OSError, subprocess.SubprocessError):
        return False


def install(exe_path=None):
    """Register the logon task and start the agent now. Returns (ok, message)."""
    if os.name != "nt":
        return False, "Auto-start is only available on Windows."
    exe = Path(exe_path or sys.executable)
    try:
        done = _powershell(install_script(exe))
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"Could not run PowerShell: {exc}"
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).strip().splitlines()
        return False, "Windows refused to create the task: " + (detail[-1] if detail else f"exit code {done.returncode}")
    return True, "The agent will start at every login (and is starting now)."


def uninstall():
    """Remove the logon task. Returns (ok, message); a task that was never installed is not an error."""
    if os.name != "nt":
        return True, "Nothing to remove."
    try:
        done = _powershell(uninstall_script())
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"Could not run PowerShell: {exc}"
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).strip().splitlines()
        return False, "Windows refused to remove the task: " + (detail[-1] if detail else f"exit code {done.returncode}")
    return True, "Auto-start was not installed." if "NOTFOUND" in done.stdout else "Auto-start removed."


def _other_copies(exe_path):
    """Other running copies of this exe. Never includes the current process, its launcher or its children."""
    me = psutil.Process()
    keep = {me.pid}
    try:
        keep.add(me.parent().pid)
        keep.update(child.pid for child in me.children(recursive=True))
    except (psutil.Error, AttributeError):
        pass
    target = os.path.normcase(str(exe_path))
    victims = []
    for proc in psutil.process_iter(["pid", "exe"]):
        try:
            if proc.info["pid"] not in keep and proc.info["exe"] and os.path.normcase(proc.info["exe"]) == target:
                victims.append(proc)
        except psutil.Error:
            continue
    return victims


def count_running_agents(exe_path):
    """How many other copies of this exe are running right now (for example the auto-start copy)."""
    return len(_other_copies(exe_path))


def stop_running_agents(exe_path):
    """End the other running copies of this exe. Returns how many were stopped."""
    victims = _other_copies(exe_path)
    for proc in victims:
        try:
            proc.terminate()
        except psutil.Error:
            pass
    psutil.wait_procs(victims, timeout=5)
    return len(victims)


def settings_files(folder):
    """The files the agent keeps next to the exe: configuration, device id, lock and logs."""
    folder = Path(folder)
    found = [folder / name for name in SETTINGS_FILES if (folder / name).exists()]
    found += sorted(folder.glob("agent.log*"))
    return found


def delete_settings(folder):
    """Delete those files. Returns (deleted, failed) lists of names."""
    deleted, failed = [], []
    for path in settings_files(folder):
        try:
            path.unlink()
            deleted.append(path.name)
        except OSError:
            failed.append(path.name)
    return deleted, failed
