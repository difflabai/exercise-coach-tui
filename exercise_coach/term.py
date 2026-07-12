"""Terminal helpers: cbreak mode, key reading, stdin draining."""

import os
import select
import sys
import termios
import tty


class WorkoutPaused(Exception):
    """Raised when user presses Ctrl-Z to suspend to shell."""
    pass


_old_term = None


def enter_cbreak() -> None:
    global _old_term
    try:
        fd = sys.stdin.fileno()
        _old_term = termios.tcgetattr(fd)
        tty.setcbreak(fd)
    except (termios.error, ValueError, OSError):
        pass


def restore_terminal() -> None:
    global _old_term
    if _old_term is not None:
        try:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, _old_term)
        except (termios.error, ValueError, OSError):
            pass
        _old_term = None


def stdin_ready() -> bool:
    try:
        return bool(select.select([sys.stdin], [], [], 0)[0])
    except (ValueError, OSError):
        return False


def drain_stdin() -> None:
    while stdin_ready():
        try:
            os.read(sys.stdin.fileno(), 1024)
        except OSError:
            break


def read_key() -> str:
    """Read a single keypress. Returns the character or 'enter' for newline/CR."""
    if not stdin_ready():
        return ""
    raw = os.read(sys.stdin.fileno(), 1024)
    if raw in (b"\n", b"\r"):
        return "enter"
    if raw == b"\x1a":
        return "ctrl-z"
    return raw.decode("utf-8", errors="ignore").lower()
