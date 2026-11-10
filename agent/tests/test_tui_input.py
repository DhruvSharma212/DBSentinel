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
