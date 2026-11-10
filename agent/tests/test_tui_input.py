"""Reading secrets when input is piped instead of typed."""
import io

import tui


def test_secret_is_read_as_a_plain_line_when_input_is_piped(monkeypatch):
    buffer = io.StringIO()
    console = tui.Console(stream=buffer, color=False, unicode=False)
    monkeypatch.setattr("sys.stdin", io.StringIO("my-secret\nsecond\n"))
    assert console.read_secret("Key: ") == "my-secret"
    assert console.read_secret("Key: ") == "second"
    assert buffer.getvalue() == "Key: Key: "


def test_a_closed_input_is_not_a_console(monkeypatch):
    class Closed(io.StringIO):
        def isatty(self):
            return False

    monkeypatch.setattr("sys.stdin", Closed(""))
    assert tui.stdin_is_console() is False
    monkeypatch.setattr("sys.stdin", None)
    assert tui.stdin_is_console() is False


def test_a_terminal_without_a_real_console_is_not_trusted_on_windows(monkeypatch):
    class Terminal(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr("sys.stdin", Terminal(""))
    if tui.os.name == "nt":
        import ctypes

        class FakeKernel:
            def GetStdHandle(self, which):
                return 0

            def GetConsoleMode(self, handle, mode):
                return 0                                   # the NUL device: not a console

        class FakeWindll:
            kernel32 = FakeKernel()

        monkeypatch.setattr(ctypes, "windll", FakeWindll(), raising=False)
        assert tui.stdin_is_console() is False
    else:
        assert tui.stdin_is_console() is True
