"""Built-in auto-start and uninstall."""
import base64
import sys
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

import agent
import autostart


class FakeRun:
    """Stands in for subprocess.run: records the command and answers with a canned result."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.result = SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append(command)
        return self.result

    def script(self):
        """The PowerShell script that was sent (it travels base64-encoded)."""
        encoded = self.calls[-1][self.calls[-1].index("-EncodedCommand") + 1]
        return base64.b64decode(encoded).decode("utf-16-le")


# ---------------------------------------------------------------- the generated script
def test_install_script_runs_the_exe_in_the_background_at_logon():
    script = autostart.install_script(r"C:\Program Files\DBSentinel\dbsentinel-agent.exe")
    assert "New-ScheduledTaskTrigger -AtLogOn" in script
    assert "-Argument '--background'" in script
    assert r"-Execute 'C:\Program Files\DBSentinel\dbsentinel-agent.exe'" in script
    assert r"-WorkingDirectory 'C:\Program Files\DBSentinel'" in script
    assert "-ExecutionTimeLimit ([TimeSpan]::Zero)" in script             # never stopped after 3 days
    assert "-RunLevel Limited" in script                                  # no administrator rights needed
    assert "Start-ScheduledTask" in script


def test_paths_with_quotes_cannot_break_the_script():
    script = autostart.install_script(r"C:\Users\O'Brien\agent\dbsentinel-agent.exe")
    assert r"C:\Users\O''Brien\agent\dbsentinel-agent.exe" in script
    assert autostart._quote("a'b'c") == "a''b''c"


def test_the_task_name_can_be_overridden_for_tests(monkeypatch):
    monkeypatch.delenv("DBSENTINEL_TASK_NAME", raising=False)
    assert autostart.task_name() == "DBSentinel Agent"
    monkeypatch.setenv("DBSENTINEL_TASK_NAME", "DBSentinel Agent TEST")
    assert "'DBSentinel Agent TEST'" in autostart.install_script("x.exe")
    assert "'DBSentinel Agent TEST'" in autostart.uninstall_script()


def test_scripts_travel_encoded(monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(autostart.subprocess, "run", fake)
    autostart._powershell("Write-Output 'héllo wörld'")
    assert fake.script() == "Write-Output 'héllo wörld'"
    assert "-EncodedCommand" in fake.calls[0] and "-NoProfile" in fake.calls[0]


# ---------------------------------------------------------------- results
@pytest.mark.skipif(sys.platform != "win32", reason="auto-start is a Windows feature")
class TestOnWindows:
    def test_install_reports_success_and_failure(self, monkeypatch):
        monkeypatch.setattr(autostart.subprocess, "run", FakeRun(0))
        ok, message = autostart.install(Path("C:/x/dbsentinel-agent.exe"))
        assert ok and "every login" in message
        monkeypatch.setattr(autostart.subprocess, "run", FakeRun(1, stderr="line1\nAccess is denied.\n"))
        ok, message = autostart.install(Path("C:/x/dbsentinel-agent.exe"))
        assert not ok and "Access is denied." in message

    def test_a_missing_powershell_is_reported_not_raised(self, monkeypatch):
        def boom(*a, **k):
            raise FileNotFoundError("powershell")
        monkeypatch.setattr(autostart.subprocess, "run", boom)
        ok, message = autostart.install(Path("C:/x/a.exe"))
        assert not ok and "PowerShell" in message
        ok, message = autostart.uninstall()
        assert not ok

    def test_uninstall_distinguishes_removed_from_never_installed(self, monkeypatch):
        monkeypatch.setattr(autostart.subprocess, "run", FakeRun(0, stdout="REMOVED\n"))
        assert autostart.uninstall() == (True, "Auto-start removed.")
        monkeypatch.setattr(autostart.subprocess, "run", FakeRun(0, stdout="NOTFOUND\n"))
        assert autostart.uninstall() == (True, "Auto-start was not installed.")
        monkeypatch.setattr(autostart.subprocess, "run", FakeRun(1, stderr="denied"))
        assert autostart.uninstall()[0] is False

    def test_is_installed(self, monkeypatch):
        monkeypatch.setattr(autostart.subprocess, "run", FakeRun(0, stdout="YES\n"))
        assert autostart.is_installed() is True
        monkeypatch.setattr(autostart.subprocess, "run", FakeRun(0, stdout="NO\n"))
        assert autostart.is_installed() is False


# ---------------------------------------------------------------- stopping running agents
class FakeProc:
    def __init__(self, pid, exe):
        self.pid, self.info, self.terminated = pid, {"pid": pid, "exe": exe}, False

    def terminate(self):
        self.terminated = True


def test_only_other_copies_of_this_exe_are_stopped(monkeypatch):
    """Safety: never stops the current process, its launcher, its children, or any other program."""
    me = psutil.Process()
    exe = r"C:\Agent\dbsentinel-agent.exe"
    mine, launcher = FakeProc(me.pid, exe), FakeProc(me.parent().pid, exe)
    other_copy, task_copy = FakeProc(900001, exe), FakeProc(900002, exe.upper())
    unrelated, no_exe = FakeProc(900003, r"C:\Windows\notepad.exe"), FakeProc(900004, None)
    procs = [mine, launcher, other_copy, task_copy, unrelated, no_exe]
    monkeypatch.setattr(autostart.psutil, "process_iter", lambda attrs=None: iter(procs))
    monkeypatch.setattr(autostart.psutil, "wait_procs", lambda victims, timeout=None: None)
    monkeypatch.setattr(autostart.os.path, "normcase", lambda p: p.lower())
    stopped = autostart.stop_running_agents(exe)
    assert stopped == 2 and other_copy.terminated and task_copy.terminated
    assert not (mine.terminated or launcher.terminated or unrelated.terminated or no_exe.terminated)


# ---------------------------------------------------------------- saved settings
def test_only_the_agents_own_files_are_listed_and_deleted(tmp_path):
    for name in ("agent.config.json", "agent.state.json", "agent.lock", "agent.log", "agent.log.1", "agent.log.2",
                 "dbsentinel-agent.exe", "Uninstall.bat", "notes.txt"):
        (tmp_path / name).write_text("x")
    assert {p.name for p in autostart.settings_files(tmp_path)} == {
        "agent.config.json", "agent.state.json", "agent.lock", "agent.log", "agent.log.1", "agent.log.2"}
    deleted, failed = autostart.delete_settings(tmp_path)
    assert len(deleted) == 6 and failed == []
    assert sorted(p.name for p in tmp_path.iterdir()) == ["Uninstall.bat", "dbsentinel-agent.exe", "notes.txt"]
    assert autostart.settings_files(tmp_path) == []


def test_a_file_that_cannot_be_deleted_is_reported(tmp_path, monkeypatch):
    (tmp_path / "agent.config.json").write_text("x")
    (tmp_path / "agent.state.json").write_text("x")
    real_unlink = Path.unlink

    def picky(self, *a, **k):
        if self.name == "agent.state.json":
            raise PermissionError("in use")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", picky)
    deleted, failed = autostart.delete_settings(tmp_path)
    assert deleted == ["agent.config.json"] and failed == ["agent.state.json"]


# ---------------------------------------------------------------- the --uninstall command
@pytest.fixture
def uninstall_env(tmp_path, monkeypatch):
    for name in ("agent.config.json", "agent.state.json", "agent.log"):
        (tmp_path / name).write_text("x")
    calls = []
    monkeypatch.setattr(agent, "app_dir", lambda: tmp_path)
    monkeypatch.setattr(agent.autostart, "uninstall", lambda: calls.append("uninstall") or (True, "Auto-start removed."))
    monkeypatch.setattr(agent.autostart, "stop_running_agents", lambda exe: calls.append("stop") or 1)
    return tmp_path, calls


def test_uninstall_with_yes_removes_everything_without_asking(uninstall_env, monkeypatch, capsys):
    folder, calls = uninstall_env
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("must not ask with --yes"))
    assert agent.cmd_uninstall(True) == 0
    assert list(folder.iterdir()) == [] and calls == ["uninstall"]          # not frozen: no process is stopped
    assert "Deleted" in capsys.readouterr().out


def test_uninstall_asks_before_deleting_and_respects_no(uninstall_env, monkeypatch):
    folder, _ = uninstall_env
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert agent.cmd_uninstall(False) == 0
    assert len(list(folder.iterdir())) == 3


def test_uninstall_default_answer_is_to_delete(uninstall_env, monkeypatch):
    folder, _ = uninstall_env
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    agent.cmd_uninstall(False)
    assert list(folder.iterdir()) == []


def test_uninstall_without_anyone_to_answer_keeps_the_files(uninstall_env, monkeypatch):
    folder, _ = uninstall_env

    def eof(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert agent.cmd_uninstall(False) == 0
    assert len(list(folder.iterdir())) == 3


def test_the_packaged_exe_also_stops_running_agents(uninstall_env, monkeypatch, capsys):
    _, calls = uninstall_env
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    agent.cmd_uninstall(True)
    assert calls == ["uninstall", "stop"] and "Stopped 1 running agent." in capsys.readouterr().out


def test_a_failed_task_removal_is_reported_in_the_exit_code(uninstall_env, monkeypatch):
    monkeypatch.setattr(agent.autostart, "uninstall", lambda: (False, "Windows refused to remove the task: denied"))
    assert agent.cmd_uninstall(True) == 1


def test_install_autostart_command_needs_the_packaged_exe(monkeypatch, capsys):
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert agent.cmd_install_autostart() == 1
    assert "packaged" in capsys.readouterr().out
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(agent.autostart, "install", lambda exe: (True, "ok"))
    assert agent.cmd_install_autostart() == 0


def test_no_helper_files_are_needed_next_to_the_exe():
    folder = Path(agent.__file__).resolve().parent
    for name in ("Uninstall.bat", "install-autostart.ps1", "uninstall-autostart.ps1", "make-package.ps1"):
        assert not (folder / name).exists(), f"{name} should not exist: the exe does this itself"


# ---------------------------------------------------------------- counting running copies
def test_running_copies_are_counted_without_counting_this_process(monkeypatch):
    me = psutil.Process()
    exe = r"C:\Agent\dbsentinel-agent.exe"
    procs = [FakeProc(me.pid, exe), FakeProc(me.parent().pid, exe), FakeProc(900001, exe), FakeProc(900002, r"C:\Windows\notepad.exe")]
    monkeypatch.setattr(autostart.psutil, "process_iter", lambda attrs=None: iter(procs))
    monkeypatch.setattr(autostart.os.path, "normcase", lambda p: p.lower())
    assert autostart.count_running_agents(exe) == 1
    assert not any(p.terminated for p in procs)                              # counting never stops anything


# ---------------------------------------------------------------- the menu of an already set-up exe
class MenuEnv:
    def __init__(self, monkeypatch, tmp_path, answers, installed=False, running=0):
        self.answers, self.calls, self.installed, self.running = list(answers), [], installed, running
        (tmp_path / "agent.config.json").write_text('{"api_url": "https://x.example", "api_key": "k", "device_name": "Rahul"}', encoding="utf-8")
        self.args = SimpleNamespace(config=tmp_path / "agent.config.json")
        monkeypatch.setattr("builtins.input", self._input)
        monkeypatch.setattr(agent.autostart, "is_installed", lambda: self.installed)
        monkeypatch.setattr(agent.autostart, "count_running_agents", lambda exe: self.running)
        monkeypatch.setattr(agent.autostart, "install", lambda exe: self.calls.append("install") or (True, "on"))
        monkeypatch.setattr(agent.autostart, "uninstall", lambda: self.calls.append("uninstall") or (True, "off"))
        monkeypatch.setattr(agent, "run_wizard", lambda args: self.calls.append("wizard") or "done")
        monkeypatch.setattr(agent, "cmd_uninstall", lambda yes: self.calls.append("cmd_uninstall") or 0)

    def _input(self, prompt=""):
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)


def test_the_menu_shows_the_current_state(monkeypatch, tmp_path, capsys):
    MenuEnv(monkeypatch, tmp_path, [""], installed=True, running=1)
    assert agent.show_menu(MenuEnv(monkeypatch, tmp_path, [""], installed=True, running=1).args) == "quit"
    out = capsys.readouterr().out
    assert "Rahul" in out and "https://x.example" in out and "Auto-start" in out and "on" in out and "in the background" in out
    for text in ("Run the agent in this window", "Turn auto-start off", "Change settings", "Uninstall", "Quit"):
        assert text in out


def test_the_menu_offers_the_opposite_of_the_current_auto_start_state(monkeypatch, tmp_path, capsys):
    env = MenuEnv(monkeypatch, tmp_path, [""], installed=False)
    agent.show_menu(env.args)
    assert "Turn auto-start on" in capsys.readouterr().out


@pytest.mark.parametrize("answer", ["", "5", "q", "Quit"])
def test_enter_and_quit_leave_without_doing_anything(monkeypatch, tmp_path, answer):
    env = MenuEnv(monkeypatch, tmp_path, [answer])
    assert agent.show_menu(env.args) == "quit" and env.calls == []


def test_choice_1_runs_the_agent_unless_one_is_already_running(monkeypatch, tmp_path, capsys):
    assert agent.show_menu(MenuEnv(monkeypatch, tmp_path, ["1"]).args) == "run"
    env = MenuEnv(monkeypatch, tmp_path, ["1", "5"], running=1)
    assert agent.show_menu(env.args) == "quit"
    assert "already running" in capsys.readouterr().out


def test_choice_2_toggles_auto_start_and_stays_in_the_menu(monkeypatch, tmp_path):
    env = MenuEnv(monkeypatch, tmp_path, ["2", "5"], installed=False)
    assert agent.show_menu(env.args) == "quit" and env.calls == ["install"]
    env = MenuEnv(monkeypatch, tmp_path, ["2", "5"], installed=True)
    agent.show_menu(env.args)
    assert env.calls == ["uninstall"]


def test_choice_3_reruns_the_setup_and_returns_to_the_menu(monkeypatch, tmp_path):
    env = MenuEnv(monkeypatch, tmp_path, ["3", "5"])
    assert agent.show_menu(env.args) == "quit" and env.calls == ["wizard"]
    env = MenuEnv(monkeypatch, tmp_path, ["3"])
    monkeypatch.setattr(agent, "run_wizard", lambda args: "run")                # the setup asked to start the agent
    assert agent.show_menu(env.args) == "run"


def test_choice_4_uninstalls_and_leaves(monkeypatch, tmp_path):
    env = MenuEnv(monkeypatch, tmp_path, ["4"])
    assert agent.show_menu(env.args) == "quit" and env.calls == ["cmd_uninstall"]


def test_unknown_answers_are_asked_again_and_closed_input_quits(monkeypatch, tmp_path, capsys):
    env = MenuEnv(monkeypatch, tmp_path, ["9", "banana"])                        # then input runs out (EOF)
    assert agent.show_menu(env.args) == "quit" and env.calls == []
    assert capsys.readouterr().out.count("choose a number from 1 to 5") == 2


def test_the_menu_only_appears_for_a_person_using_the_packaged_exe(monkeypatch, tmp_path):
    """execute() shows it for: packaged + real console + no flags + config and device id already there."""
    shown = []
    monkeypatch.setattr(agent, "show_menu", lambda args: shown.append(1) or "quit")
    monkeypatch.setattr(agent, "app_dir", lambda: tmp_path)
    monkeypatch.setattr(agent.tui, "stdin_is_console", lambda: True)
    (tmp_path / "agent.config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "agent.state.json").write_text("{}", encoding="utf-8")

    def args(**flags):
        base = dict(once=False, dry_run=False, background=False, setup=False, config=tmp_path / "agent.config.json")
        return SimpleNamespace(**{**base, **flags})

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert agent.execute(args()) == 0 and shown == [1]                          # packaged, interactive, no flags: menu
    for flags in ({"once": True}, {"background": True}, {"setup": True}, {"dry_run": True}):
        shown.clear()
        try:
            agent.execute(args(**flags))
        except Exception:
            pass                                                                  # the rest of the flow is not under test here
        assert shown == [], flags
    monkeypatch.delattr(sys, "frozen", raising=False)                           # running from source: never a menu
    shown.clear()
    try:
        agent.execute(args())
    except Exception:
        pass
    assert shown == []
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    (tmp_path / "agent.state.json").unlink()                                    # first launch: no menu, enroll and start
    try:
        agent.execute(args())
    except Exception:
        pass
    assert shown == []


def test_the_working_directory_comes_from_a_windows_path_on_any_os():
    """CI runs on Linux, where a plain Path does not split a Windows path at its backslashes."""
    script = autostart.install_script(r"D:\Tools\My Agent\dbsentinel-agent.exe")
    assert r"-WorkingDirectory 'D:\Tools\My Agent'" in script
    assert r"-Execute 'D:\Tools\My Agent\dbsentinel-agent.exe'" in script
    from pathlib import PurePosixPath
    assert PurePosixPath(r"D:\Tools\My Agent\dbsentinel-agent.exe").parent == PurePosixPath(".")   # the trap this avoids
