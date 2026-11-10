"""Terminal presentation for the agent: colour, layout, prompts and a progress spinner.

Colours match the dashboard (same cyan accent and status colours). Output degrades gracefully:
no colour when NO_COLOR is set or output is not a terminal, plain ASCII when Unicode is not available.
"""
import getpass
import os
import shutil
import sys
import threading
import time
from contextlib import contextmanager

# Dashboard palette (RGB) and the nearest basic ANSI colours for terminals without truecolor.
RGB = {
    "accent": (67, 217, 232),
    "ok": (54, 211, 153),
    "warn": (245, 173, 98),
    "err": (251, 113, 133),
    "muted": (113, 131, 155),
    "text": (231, 237, 247),
    "rule": (52, 66, 88),
}
BASIC = {"accent": "36", "ok": "32", "warn": "33", "err": "31", "muted": "90", "text": "97", "rule": "90"}

GLYPHS = {
    True: {"rule": "─", "v": "│", "tl": "┌", "tr": "┐", "bl": "└", "br": "┘", "dot": "●", "arrow": "›", "mask": "•", "dash": "·"},
    False: {"rule": "-", "v": "|", "tl": "+", "tr": "+", "bl": "+", "br": "+", "dot": "*", "arrow": ">", "mask": "*", "dash": "-"},
}
SPIN_FRAMES = "|/-\\"
ESC = "\x1b"


def enable_vt():
    """Turn on ANSI escape handling in the Windows console. Returns True when colours can be used."""
    if os.name != "nt":
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
    except Exception:
        return False


def truecolor_supported():
    if os.environ.get("COLORTERM") in ("truecolor", "24bit") or os.environ.get("WT_SESSION"):
        return True
    if os.name == "nt":
        try:
            return sys.getwindowsversion().build >= 14931  # Windows 10 Anniversary Update and later
        except Exception:
            return False
    return False


def _set_utf8(stream):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:
            pass


class Console:
    def __init__(self, stream=None, color=None, unicode=None, width=None):
        self.out = stream if stream is not None else sys.stdout
        is_tty = bool(getattr(self.out, "isatty", lambda: False)())
        if color is None:
            color = is_tty and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb" and enable_vt()
        self.color = bool(color)
        self.truecolor = self.color and truecolor_supported()
        self.unicode = is_tty if unicode is None else bool(unicode)
        if self.unicode and is_tty:
            _set_utf8(self.out)
        self.g = GLYPHS[self.unicode]
        columns = width or shutil.get_terminal_size((80, 24)).columns
        self.width = max(52, min(columns - 4, 74))

    # ---------------------------------------------------------------- low level
    def paint(self, text, role, bold=False):
        if not self.color:
            return text
        weight = "1;" if bold else ""
        if self.truecolor:
            r, g, b = RGB[role]
            return f"{ESC}[{weight}38;2;{r};{g};{b}m{text}{ESC}[0m"
        return f"{ESC}[{weight}{BASIC[role]}m{text}{ESC}[0m"

    def write(self, text="", end="\n"):
        self.out.write(text + end)
        self.out.flush()

    def line(self, text=""):
        self.write("  " + text if text else "")

    def tail(self, text, size):
        """Shorten from the left so the end (for example a file name) stays visible."""
        if len(text) <= size:
            return text
        mark = "…" if self.unicode else "..."
        return mark + text[len(text) - (size - len(mark)):]

    def _fit(self, text, size):
        return text if len(text) <= size else text[: max(0, size - 1)] + "…" if self.unicode else text[: max(0, size - 3)] + "..."

    # ---------------------------------------------------------------- structure
    def header(self, title, right=""):
        left_plain = f"DBSentinel {self.g['dash']} {title}"
        gap = max(2, self.width - len(left_plain) - len(right))
        self.write()
        self.line(self.paint("DBSentinel", "accent", bold=True) + self.paint(f" {self.g['dash']} ", "muted") + self.paint(title, "text", bold=True)
                  + " " * gap + self.paint(right, "muted"))
        self.line(self.paint(self.g["rule"] * self.width, "rule"))

    def section(self, number, title):
        label = f" {number} {self.g['dash']} {title} "
        tail = max(2, self.width - len(label) - 2)
        self.write()
        self.line(self.paint(self.g["rule"] * 2, "rule") + self.paint(f" {number} ", "accent", bold=True)
                  + self.paint(f"{self.g['dash']} ", "muted") + self.paint(f"{title} ", "text", bold=True) + self.paint(self.g["rule"] * tail, "rule"))

    def paragraph(self, text):
        words, row = text.split(), ""
        for word in words:
            if row and len(row) + 1 + len(word) > self.width:
                self.line(self.paint(row, "muted"))
                row = word
            else:
                row = f"{row} {word}".strip()
        if row:
            self.line(self.paint(row, "muted"))

    # ---------------------------------------------------------------- messages
    def _status(self, role, text):
        self.line(self.paint(self.g["dot"], role) + " " + text)

    def ok(self, text):
        self._status("ok", text)

    def warn(self, text):
        self._status("warn", text)

    def fail(self, text):
        self._status("err", text)

    def note(self, text):
        self.line("  " + self.paint(text, "muted"))

    def problem(self, text):
        self.line("  " + self.paint("! " + text, "warn"))

    def kv(self, rows):
        label_width = max((len(k) for k, _ in rows), default=0)
        for key, value in rows:
            self.line(self.paint(key.ljust(label_width), "muted") + "  " + self._fit(str(value), self.width - label_width - 2))

    def panel(self, title, rows):
        inner = self.width - 2
        label_width = max((len(k) for k, _ in rows), default=0)
        self.write()
        top = f" {title} "
        self.line(self.paint(self.g["tl"], "rule") + self.paint(top, "accent", bold=True)
                  + self.paint(self.g["rule"] * (inner - len(top)) + self.g["tr"], "rule"))
        for key, value in rows:
            value = self._fit(str(value), inner - label_width - 4)
            padding = " " * max(0, inner - (1 + label_width + 2 + len(value)))
            self.line(self.paint(self.g["v"], "rule") + " " + self.paint(key.ljust(label_width), "muted") + "  "
                      + value + padding + self.paint(self.g["v"], "rule"))
        self.line(self.paint(self.g["bl"] + self.g["rule"] * inner + self.g["br"], "rule"))

    # ---------------------------------------------------------------- prompts
    def prompt_text(self, label, default=None, hint=None):
        text = "  " + self.paint(self.g["arrow"], "accent", bold=True) + " " + self.paint(label, "text", bold=True)
        if hint:
            text += self.paint(f" ({hint})", "muted")
        if default:
            text += self.paint(f" [{default}]", "muted")
        return text + " "

    def yes_no_text(self, label, default):
        suffix = "Y/n" if default else "y/N"
        return "  " + self.paint(self.g["arrow"], "accent", bold=True) + " " + self.paint(label, "text", bold=True) + self.paint(f" [{suffix}]", "muted") + " "

    def read_secret(self, prompt):
        """Read a secret and echo one mask character per key, so the user can see that typing registers."""
        typed = sys.stdin is not None and getattr(sys.stdin, "isatty", lambda: False)()
        if not typed:                          # input is piped from a file or another program: nothing to hide
            self.out.write(prompt)
            self.out.flush()
            return (sys.stdin.readline() if sys.stdin is not None else "").strip()
        if os.name != "nt":
            return getpass.getpass(prompt)
        import msvcrt
        self.out.write(prompt)
        self.out.flush()
        chars = []
        while True:
            ch = msvcrt.getwch()
            if ch in ("\r", "\n"):
                self.out.write("\n")
                return "".join(chars)
            if ch == "\x03":
                raise KeyboardInterrupt
            if ch in ("\x00", "\xe0"):          # arrow / function keys send a prefix, then a code
                msvcrt.getwch()
                continue
            if ch in ("\b", "\x7f"):
                if chars:
                    chars.pop()
                    self.out.write("\b \b")
            elif ch.isprintable():
                chars.append(ch)
                self.out.write(self.paint(self.g["mask"], "muted"))
            self.out.flush()

    # ---------------------------------------------------------------- progress
    @contextmanager
    def spinner(self, label):
        """Shows `label` with a spinner while work runs. Call .finish(ok, text) on the yielded object."""
        box = _Spinner(self, label)
        box.start()
        try:
            yield box
        finally:
            box.stop()


class _Spinner:
    def __init__(self, console, label):
        self.console, self.label = console, label
        self._stop = threading.Event()
        self._thread = None
        self._animated = console.color and getattr(console.out, "isatty", lambda: False)()

    def start(self):
        if not self._animated:
            self.console.line(self.console.paint(self.console.g["dash"], "muted") + " " + self.label + " ...")
            return
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def _spin(self):
        i = 0
        while not self._stop.is_set():
            frame = self.console.paint(SPIN_FRAMES[i % len(SPIN_FRAMES)], "accent", bold=True)
            self.console.out.write(f"\r{ESC}[2K  {frame} {self.label}")
            self.console.out.flush()
            i += 1
            time.sleep(0.12)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1)
            self.console.out.write(f"\r{ESC}[2K")
            self.console.out.flush()

    def finish(self, ok, text):
        self.stop()
        (self.console.ok if ok else self.console.fail)(text)


class ConsoleFormatter:
    """Log lines for the console: `10:48:49  INFO   message`, with the level coloured."""

    LEVEL_ROLE = {"DEBUG": "muted", "INFO": "accent", "WARNING": "warn", "ERROR": "err", "CRITICAL": "err"}

    def __init__(self, console):
        self.console = console

    def format(self, record):
        stamp = time.strftime("%H:%M:%S", time.localtime(record.created))
        level = self.console.paint(f"{record.levelname:<7}", self.LEVEL_ROLE.get(record.levelname, "text"))
        text = record.getMessage()
        if record.exc_info:
            import logging
            text += "\n" + logging.Formatter().formatException(record.exc_info)
        return f"{self.console.paint(stamp, 'muted')}  {level} {text}"
