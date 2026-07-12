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
        # setcbreak leaves ISIG enabled, so without this the kernel consumes
        # Ctrl-Z as SUSP and stops the process dead — read_key would never see
        # byte 0x1a and the graceful save-and-suspend path could never run.
        # Disable just the SUSP character (0 = _POSIX_VDISABLE on Linux/macOS)
        # so Ctrl-Z becomes a readable key while Ctrl-C keeps raising
        # KeyboardInterrupt via ISIG.
        mode = termios.tcgetattr(fd)
        mode[6][termios.VSUSP] = b"\x00"
        termios.tcsetattr(fd, termios.TCSADRAIN, mode)
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
    # Type-ahead already split into keys must be flushed with the raw bytes.
    _pending_keys.clear()
    while stdin_ready():
        try:
            os.read(sys.stdin.fileno(), 1024)
        except OSError:
            break


# Named key sequences ("\x1bO..." are the application-cursor-mode variants).
# A lone \x1b is the Escape key itself.
_KEY_NAMES = {
    b"\n": "enter", b"\r": "enter",
    b"\x1a": "ctrl-z",
    b"\x1b": "esc",
    b"\x1b[A": "up", b"\x1b[B": "down", b"\x1b[C": "right", b"\x1b[D": "left",
    b"\x1bOA": "up", b"\x1bOB": "down", b"\x1bOC": "right", b"\x1bOD": "left",
}

# Escape sequences, longest first, so "\x1b[A" wins over its "\x1b" prefix.
_ESC_SEQS = sorted(
    (seq for seq in _KEY_NAMES if len(seq) > 1 and seq.startswith(b"\x1b")),
    key=len, reverse=True,
)

# Keys split from a chunk but not yet returned by read_key().
_pending_keys: list[str] = []


def split_keys(raw: bytes) -> list[str]:
    """Split one os.read chunk into individual keypresses.

    Key auto-repeat (or two presses within one poll window) coalesces several
    escape sequences / characters into a single read; matching the whole chunk
    at once would silently drop every key in it. Unknown CSI/SS3 sequences
    (Home, F-keys, ...) are consumed through their final byte and dropped so
    their payload never leaks in as ordinary characters."""
    keys: list[str] = []
    i, n = 0, len(raw)
    while i < n:
        if raw[i] == 0x1B:
            for seq in _ESC_SEQS:
                if raw.startswith(seq, i):
                    keys.append(_KEY_NAMES[seq])
                    i += len(seq)
                    break
            else:
                if raw[i + 1:i + 2] in (b"[", b"O"):
                    # Unknown escape sequence: skip parameter bytes up to the
                    # final byte (0x40-0x7E) and drop the whole thing.
                    j = i + 2
                    while j < n and not 0x40 <= raw[j] <= 0x7E:
                        j += 1
                    i = j + 1
                else:
                    keys.append("esc")
                    i += 1
            continue
        one = raw[i:i + 1]
        if one in _KEY_NAMES:
            keys.append(_KEY_NAMES[one])
            i += 1
            continue
        # Plain text run up to the next special byte: one key per decoded
        # character (multi-byte UTF-8 stays a single key).
        j = i + 1
        while j < n and raw[j] != 0x1B and raw[j:j + 1] not in _KEY_NAMES:
            j += 1
        keys.extend(raw[i:j].decode("utf-8", errors="ignore").lower())
        i = j
    return keys


def read_key() -> str:
    """Read a single keypress. Returns the character, or a name for special
    keys: 'enter', 'esc', 'ctrl-z', 'up'/'down'/'left'/'right'. When one read
    chunk holds several keypresses the extras are queued for later calls."""
    if _pending_keys:
        return _pending_keys.pop(0)
    if not stdin_ready():
        return ""
    raw = os.read(sys.stdin.fileno(), 1024)
    keys = split_keys(raw)
    if not keys:
        return ""
    _pending_keys.extend(keys[1:])
    return keys[0]
