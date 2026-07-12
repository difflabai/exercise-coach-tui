"""Persisted user settings: ~/.config/exercise-coach/config.toml + merge-on-save.

One settings file shared by multiple features: audio.py's AudioSettings
delegates its volume/mute persistence here, and the buddy flag, learned
paces, preferred piper voice, and rest default live here directly. Every
write merges into whatever is already on disk — so persisting one key never
clobbers another (e.g. a 'm' press during a one-run --volume override must
not overwrite the saved volume), and unknown keys a user added by hand
always survive.

The file was settings.json in the XDG data dir before recommendations §7
promoted it to config.toml in the XDG config dir; `migrate_legacy_settings`
converts the old file once (and leaves it in place). Python ships tomllib
for reading but no writer, so `dumps_toml` below is a tiny hand-rolled
serializer for the value space this app (and tomllib itself) can produce.
"""

import datetime
import json
import math
import os
import re
import tomllib
from pathlib import Path

DEFAULT_REST = 75


def config_file() -> Path:
    """The config.toml path ($XDG_CONFIG_HOME or ~/.config). Read live from
    the environment on every call — tests re-point XDG_CONFIG_HOME per test."""
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "exercise-coach" / "config.toml"


def legacy_settings_file() -> Path:
    """Where settings lived before config.toml: settings.json in the data dir."""
    # Lazy import: state.py's DATA_DIR is patched in tests, read it live.
    from . import state
    return state.DATA_DIR / "settings.json"


# ---------------------------------------------------------------------------
# TOML writing (stdlib has tomllib for reading only)
# ---------------------------------------------------------------------------

_BARE_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")
_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\f": "\\f",
            "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _toml_escape(s: str) -> str:
    out = []
    for ch in s:
        if ch in _ESCAPES:
            out.append(_ESCAPES[ch])
        elif ord(ch) < 0x20 or ch == "\x7f":
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return "".join(out)


def _toml_key(key: str) -> str:
    """Bare key when allowed, else a quoted key (exercise names in [paces])."""
    if _BARE_KEY_RE.fullmatch(key):
        return key
    return f'"{_toml_escape(key)}"'


def _toml_value(value) -> str:
    if isinstance(value, bool):  # before int: bool is an int subclass
        return "true" if value else "false"
    if isinstance(value, float) and not math.isfinite(value):
        return "nan" if math.isnan(value) else ("inf" if value > 0 else "-inf")
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return f'"{_toml_escape(value)}"'
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()  # unquoted date-times are valid TOML
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict):  # only reached for dicts nested inside arrays
        inner = ", ".join(f"{_toml_key(k)} = {_toml_value(v)}" for k, v in value.items())
        return "{" + inner + "}"
    raise TypeError(f"cannot serialize {type(value).__name__} to TOML")


def _emit_table(path: list[str], table: dict, out: list[str]) -> None:
    scalars = [(k, v) for k, v in table.items() if not isinstance(v, dict)]
    subtables = [(k, v) for k, v in table.items() if isinstance(v, dict)]
    if path and (scalars or not subtables):
        # A table holding only sub-tables needs no header of its own — the
        # dotted sub-table headers recreate it implicitly on read.
        out.append("")
        out.append("[" + ".".join(_toml_key(p) for p in path) + "]")
    for key, value in scalars:
        out.append(f"{_toml_key(key)} = {_toml_value(value)}")
    for key, value in subtables:
        _emit_table(path + [key], value, out)


def dumps_toml(data: dict) -> str:
    """Serialize a dict to TOML text tomllib reads back identically.

    Value space: str/int/float/bool scalars, lists, and dicts as (nested)
    tables — this app writes only scalars plus the one [paces] int table,
    but merge-on-save must round-trip whatever a user hand-added too."""
    out: list[str] = []
    _emit_table([], data, out)
    while out and out[0] == "":
        out.pop(0)
    return "\n".join(out) + "\n" if out else ""


# ---------------------------------------------------------------------------
# Load / merge-on-save
# ---------------------------------------------------------------------------

def load_data() -> dict:
    """Read config.toml as a dict. Missing/corrupt files -> {}.

    ValueError covers both tomllib.TOMLDecodeError (bad TOML) and
    UnicodeDecodeError (non-UTF-8 bytes, e.g. a write truncated mid-way
    through a multibyte pace name) — either way the file is corrupt and the
    next save_data replaces it; a broken config must never brick startup."""
    try:
        with open(config_file(), "rb") as f:
            return tomllib.load(f)
    except (OSError, ValueError):
        return {}


def save_data(updates: dict) -> None:
    """Merge `updates` into config.toml, replacing a corrupt file.

    Read-modify-write so unknown keys survive every save.
    Best-effort: failure must never interrupt a workout."""
    try:
        path = config_file()
        data = load_data()
        data.update(updates)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dumps_toml(data), encoding="utf-8")
    except (OSError, TypeError, ValueError):
        pass


def _strip_nones(value):
    """Drop JSON nulls recursively — TOML has no null, and one stray null
    must not abort the migration of every other key."""
    if isinstance(value, dict):
        return {k: _strip_nones(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_strip_nones(v) for v in value if v is not None]
    return value


def migrate_legacy_settings() -> None:
    """One-time migration: the old settings.json -> config.toml.

    Only runs while config.toml does not exist; the old settings.json is
    read once, converted, and left in place (a harmless orphan — user data
    is never deleted). Best-effort like every other settings write."""
    try:
        cfg = config_file()
        if cfg.exists():
            return
        legacy = legacy_settings_file()
        if not legacy.exists():
            return
        data = json.loads(legacy.read_text())
        if not isinstance(data, dict):
            return
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(dumps_toml(_strip_nones(data)), encoding="utf-8")
    except (OSError, TypeError, ValueError):
        pass


# ---------------------------------------------------------------------------
# Preferred piper voice / rest default (read-only accessors for cli startup)
# ---------------------------------------------------------------------------

def preferred_voice() -> str | None:
    """The persisted piper voice filename ("voice" key), or None.

    cli.main feeds it into tts.PIPER_PREFERRED_VOICE so _piper_model's
    preferred-voice scan picks it up."""
    value = load_data().get("voice")
    if isinstance(value, str) and value:
        return value
    return None


def rest_default() -> int:
    """The persisted fallback rest seconds ("rest_default" key).

    Used when neither the cassette nor --rest provides one — the flag and a
    JSON cassette's own rest always override it. Missing/invalid -> 75."""
    value = load_data().get("rest_default")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return DEFAULT_REST


# ---------------------------------------------------------------------------
# Learned per-exercise pace (median rep-set seconds, for ETAs)
# ---------------------------------------------------------------------------

# Newest-last insertion order; update_paces evicts from the front past the cap.
MAX_PACES = 200

paces: dict[str, int] = {}


def _read_paces() -> dict[str, int]:
    """The persisted paces map, insertion order kept, invalid entries dropped."""
    value = load_data().get("paces")
    if not isinstance(value, dict):
        return {}
    # Filter on the *stored* value: TOML happily yields inf/nan (int() would
    # raise) and floats in (0, 1) (int() would store a 0 pace), so require a
    # finite value whose int() is still positive — the same int(secs) > 0
    # contract update_paces enforces.
    return {
        name: int(secs) for name, secs in value.items()
        if isinstance(name, str)
        and isinstance(secs, (int, float)) and not isinstance(secs, bool)
        and math.isfinite(secs) and int(secs) > 0
    }


def load_paces() -> None:
    """Load the persisted per-exercise paces into the module global."""
    global paces
    paces = _read_paces()


def update_paces(updates: dict[str, int]) -> None:
    """Merge this session's per-exercise paces into config.toml.

    Updated names are re-inserted newest-last so the MAX_PACES eviction drops
    the longest-untouched exercises, never the ones just performed."""
    global paces
    merged = _read_paces()
    for name, secs in updates.items():
        if int(secs) <= 0:  # a sub-half-second median is noise, not a pace
            continue
        merged.pop(name, None)
        merged[name] = int(secs)
    if len(merged) > MAX_PACES:
        merged = dict(list(merged.items())[-MAX_PACES:])
    paces = merged
    save_data({"paces": merged})


# ---------------------------------------------------------------------------
# Buddy on/off
# ---------------------------------------------------------------------------

buddy_enabled: bool = True


def load_buddy_enabled() -> None:
    """Load the persisted buddy flag. Missing/invalid keeps the current value."""
    global buddy_enabled
    value = load_data().get("buddy")
    if isinstance(value, bool):
        buddy_enabled = value


def set_buddy_enabled(enabled: bool) -> None:
    """Set and persist the buddy flag. Unlike --volume/--mute there is no
    runtime key for the buddy, so the CLI flags persist the choice."""
    global buddy_enabled
    buddy_enabled = enabled
    save_data({"buddy": enabled})
