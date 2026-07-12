"""Persisted user settings: settings.json path lookup + merge-on-save helpers.

Promoted from audio.py so multiple features can share one settings file:
audio.py's AudioSettings delegates its volume/mute persistence here, and the
buddy on/off flag lives here directly. The file sits in the same XDG data dir
as the state file, and every write merges into whatever is already on disk —
so persisting one key never clobbers another (e.g. a 'm' press during a
one-run --volume override must not overwrite the saved volume).
"""

import json
import math


def settings_file():
    # Lazy import: state.py's DATA_DIR is patched in tests, read it live.
    from . import state
    return state.DATA_DIR / "settings.json"


def load_data() -> dict:
    """Read settings.json as a dict. Missing/corrupt/non-object files -> {}."""
    try:
        data = json.loads(settings_file().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_data(updates: dict) -> None:
    """Merge `updates` into settings.json, replacing a corrupt/non-object file.

    Best-effort: failure must never interrupt a workout."""
    try:
        path = settings_file()
        data = load_data()
        data.update(updates)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
    except OSError:
        pass


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
    # Filter on the *stored* value: json.loads happily yields Infinity/NaN
    # (int() would raise) and floats in (0, 1) (int() would store a 0 pace),
    # so require a finite value whose int() is still positive — the same
    # int(secs) > 0 contract update_paces enforces.
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
    """Merge this session's per-exercise paces into settings.json.

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
