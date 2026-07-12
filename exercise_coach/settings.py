"""Persisted user settings: settings.json path lookup + merge-on-save helpers.

Promoted from audio.py so multiple features can share one settings file:
audio.py's AudioSettings delegates its volume/mute persistence here, and the
buddy on/off flag lives here directly. The file sits in the same XDG data dir
as the state file, and every write merges into whatever is already on disk —
so persisting one key never clobbers another (e.g. a 'm' press during a
one-run --volume override must not overwrite the saved volume).
"""

import json


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
