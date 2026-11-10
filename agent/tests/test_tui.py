"""Terminal presentation: colour decisions, layout, ASCII fallback."""
import io
import logging
import re

import tui

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def make(color=False, unicode=False, width=80, truecolor=False):
    buffer = io.StringIO()
    console = tui.Console(stream=buffer, color=color, unicode=unicode, width=width)
    console.truecolor = truecolor
    return console, buffer


def test_plain_output_has_no_escape_codes():
    console, buffer = make()
    console.header("Agent setup", "v0.2.0")
    console.section(1, "Server")
    console.ok("fine")
    console.fail("broken")
    console.panel("Summary", [("Server", "https://x.example")])
    assert "\x1b" not in buffer.getvalue()
    assert "Agent setup" in buffer.getvalue() and "v0.2.0" in buffer.getvalue()


def test_ascii_fallback_uses_no_box_drawing():
    console, buffer = make(unicode=False)
    console.header("Agent setup")
    console.panel("Summary", [("Device", "LAPTOP")])
    text = buffer.getvalue()
    assert all(ord(ch) < 128 for ch in text)
    assert "+" in text and "|" in text


def test_colour_uses_the_dashboard_palette():
    console, _ = make(color=True, truecolor=True)
    assert console.paint("x", "accent") == "\x1b[38;2;67;217;232mx\x1b[0m"
    assert console.paint("x", "ok", bold=True).startswith("\x1b[1;38;2;54;211;153m")
    basic, _ = make(color=True, truecolor=False)
    assert basic.paint("x", "err") == "\x1b[31mx\x1b[0m"


def test_no_colour_when_disabled_or_not_a_terminal(monkeypatch):
    buffer = io.StringIO()                                   # not a terminal
    assert tui.Console(stream=buffer).color is False
    monkeypatch.setenv("NO_COLOR", "1")
    assert tui.Console(stream=buffer, color=None).color is False


def test_panel_rows_are_aligned_and_fit_the_width():
    console, buffer = make(width=60)
    console.panel("Summary", [("Server", "https://dbsentinel.example.com"), ("PostgreSQL", "monitored"),
                              ("Long", "x" * 200)])
    lines = [line.strip("\n") for line in buffer.getvalue().splitlines() if line.strip()]
    widths = {len(ANSI.sub("", line)) for line in lines}
    assert len(widths) == 1, widths                           # every row, top and bottom edge the same width
    assert max(widths) <= console.width + 2


def test_paragraph_wraps_to_the_width():
    console, buffer = make(width=60)
    console.paragraph("word " * 30)
    lines = buffer.getvalue().splitlines()
    assert len(lines) > 2 and all(len(line) <= console.width + 2 for line in lines)


def test_tail_keeps_the_end_of_a_path():
    console, _ = make()
    short = console.tail(r"C:\Users\someone\AppData\Local\DBSentinel\agent.config.json", 30)
    assert len(short) == 30 and short.endswith("agent.config.json") and short.startswith("...")
    assert console.tail("short", 30) == "short"


def test_spinner_prints_progress_and_result_without_a_terminal():
    console, buffer = make()
    with console.spinner("Contacting server") as spin:
        spin.finish(True, "accepted")
    text = buffer.getvalue()
    assert "Contacting server" in text and "accepted" in text and "\r" not in text


def test_log_lines_show_time_level_and_message():
    console, _ = make()
    formatter = tui.ConsoleFormatter(console)
    record = logging.LogRecord("x", logging.WARNING, __file__, 1, "send failed (%s)", ("timeout",), None)
    line = formatter.format(record)
    assert re.match(r"\d\d:\d\d:\d\d  WARNING send failed \(timeout\)$", line)
