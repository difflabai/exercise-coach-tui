"""Shared fixtures for exercise-coach tests.

Hermetic rules enforced here:
- no test ever touches the real ~/.local/share/exercise-coach (``isolated_state``,
  autouse, plus an XDG_DATA_HOME override set *before* exercise_coach imports),
- no test ever spawns a sound/TTS subprocess (``no_audio``, autouse),
- no test needs a TTY (screens/player get an injected ``read_key``),
- no test sleeps real time (``FakeClock.sleep`` just advances the fake clock).
"""

import json
import os
import subprocess
import tempfile
import types
from dataclasses import dataclass
from io import StringIO
from pathlib import Path

# state.py resolves DATA_DIR from XDG_DATA_HOME at import time — point it at a
# throwaway dir before anything imports exercise_coach. (isolated_state then
# re-points the module constants at the per-test tmp_path.)
os.environ["XDG_DATA_HOME"] = tempfile.mkdtemp(prefix="exercise-coach-tests-xdg-")
# settings.py resolves config.toml from XDG_CONFIG_HOME live on every call,
# but set a throwaway here too so nothing can ever touch the real ~/.config
# (isolated_state then re-points it at the per-test tmp_path).
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="exercise-coach-tests-xdg-config-")

# Color-forcing env vars make Rich emit ANSI into capsys regardless of the
# fake terminals the tests set up (e.g. `FORCE_COLOR=3 pytest` broke the
# try_resume output assertions). Neutralize them for the whole test process,
# before Rich is imported anywhere; the autouse fixture below keeps them out
# for the duration of every test too (keep this list and _COLOR_ENV_VARS in
# sync).
os.environ.pop("FORCE_COLOR", None)
os.environ.pop("NO_COLOR", None)
os.environ.pop("CLICOLOR_FORCE", None)
os.environ.pop("COLORTERM", None)

import pytest
from rich.console import Console
from rich.live import Live

from exercise_coach import audio, buddy, screens, state, tts, ui
from exercise_coach import player as player_mod
from exercise_coach import settings as user_settings
from exercise_coach.cassette import load_cassette_from_dict
from exercise_coach.models import Cassette
from exercise_coach.player import Player

FIXTURES_DIR = Path(__file__).parent / "fixtures"

_COLOR_ENV_VARS = ("FORCE_COLOR", "NO_COLOR", "CLICOLOR_FORCE", "COLORTERM")


# ---------------------------------------------------------------------------
# Hermetic isolation (both autouse)
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    """Point every state/log/config path at tmp_path. Returns the data dir.

    state.py computes DATA_DIR/STATE_FILE/LOG_FILE at import time, so the
    module attributes are patched directly; all state functions read them as
    module globals. _LEGACY_DIR is patched too so migrate_legacy_files can
    never move files out of the real repo root. settings.py's config.toml
    path reads XDG_CONFIG_HOME live, so the env override is enough there.
    """
    data_dir = tmp_path / "exercise-coach-data"
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setattr(state, "DATA_DIR", data_dir)
    monkeypatch.setattr(state, "STATE_FILE", data_dir / ".workout_state.json")
    monkeypatch.setattr(state, "LOG_FILE", data_dir / "workout_log.txt")
    monkeypatch.setattr(state, "_LEGACY_DIR", tmp_path / "legacy")
    return data_dir


@pytest.fixture(autouse=True)
def neutral_color_env(monkeypatch):
    """Belt to the import-time braces above: Rich reads these env vars at
    Console() construction time, so also keep them deleted around every test
    (a test that sets one deliberately overrides this via its own patching)."""
    for var in _COLOR_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def reset_audio_settings():
    """The volume/mute singleton is process-global mutable state — reset it to
    defaults around every test so one test's key presses can't leak into the
    next. (Persistence writes land in the isolated_state tmp dir.)"""
    audio.settings.volume = 0.7
    audio.settings.muted = False
    yield
    audio.settings.volume = 0.7
    audio.settings.muted = False


_DEFAULT_PIPER_VOICE = tts.PIPER_PREFERRED_VOICE


@pytest.fixture(autouse=True)
def reset_buddy_state():
    """buddy.py's celebrate-until timestamp, settings.py's buddy flag and
    paces map, ui.py's pending-rests set, and tts.py's preferred voice are
    process-global mutable state — reset all of them around every test
    (mirrors reset_audio_settings; the player stamps celebrate() on every
    set, update_paces() at session end, binds its live _pending_rest set
    into ui.pending_rests at construction, and cli.main feeds the config
    voice into tts)."""
    buddy.reset()
    user_settings.buddy_enabled = True
    user_settings.paces = {}
    ui.pending_rests = set()
    tts.PIPER_PREFERRED_VOICE = _DEFAULT_PIPER_VOICE
    yield
    buddy.reset()
    user_settings.buddy_enabled = True
    user_settings.paces = {}
    ui.pending_rests = set()
    tts.PIPER_PREFERRED_VOICE = _DEFAULT_PIPER_VOICE


class AudioLog:
    """What would have been played/spoken during a test.

    - ``spoken``: every TTS line, in order (say/say_sync/speak all funnel here).
    - ``sounds``: marker byte-strings for each chime, in order:
      b"set_complete", b"group_complete", b"rest_done", b"rest_warning".
    """

    def __init__(self) -> None:
        self.spoken: list[str] = []
        self.sounds: list[bytes] = []


@pytest.fixture(autouse=True)
def no_audio(monkeypatch):
    """Guarantee no subprocess ever spawns for sound or TTS; record instead.

    Returns an AudioLog so tests can assert on spoken lines / chimes.
    """
    log = AudioLog()

    # Hard backstop: even unpatched code paths find no binary / cannot spawn.
    # Replace the modules' *references* to shutil/subprocess with stub
    # namespaces — never mutate the real stdlib modules (shared process-wide).
    def _no_popen(*_args, **_kwargs):
        raise FileNotFoundError("subprocess spawning is disabled in tests")

    subprocess_stub = types.SimpleNamespace(
        Popen=_no_popen,
        PIPE=subprocess.PIPE,
        DEVNULL=subprocess.DEVNULL,
        TimeoutExpired=subprocess.TimeoutExpired,
    )
    monkeypatch.setattr(tts, "shutil", types.SimpleNamespace(which=lambda _name: None))
    monkeypatch.setattr(tts, "subprocess", subprocess_stub)
    monkeypatch.setattr(audio, "subprocess", subprocess_stub)

    # Record speech at the funnel point. say()/say_sync() still set the
    # caption (so UI caption rendering stays testable) and then call
    # _start_say via module-global lookup — which now just records.
    monkeypatch.setattr(tts, "_start_say", lambda text: log.spoken.append(text))
    # No procs are ever registered, so terminate_say()/say_sync() are no-ops.

    # Record chimes; skip tone generation entirely. play_sound and the tone
    # functions were imported by name into player.py / screens.py, so patch
    # those references as well as the source module.
    def _record_sound(sound_data: bytes) -> None:
        log.sounds.append(sound_data)

    monkeypatch.setattr(audio, "play_sound", _record_sound)
    monkeypatch.setattr(player_mod, "play_sound", _record_sound)
    monkeypatch.setattr(screens, "play_sound", _record_sound)
    monkeypatch.setattr(player_mod, "sound_set_complete", lambda: b"set_complete")
    monkeypatch.setattr(player_mod, "sound_group_complete", lambda: b"group_complete")
    monkeypatch.setattr(screens, "sound_rest_done", lambda: b"rest_done")
    monkeypatch.setattr(screens, "sound_rest_warning", lambda: b"rest_warning")

    return log


# ---------------------------------------------------------------------------
# Fake clock + scripted keys
# ---------------------------------------------------------------------------

class FakeClock:
    """Injectable now/sleep pair. sleep() just advances the clock."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.t = start

    def now(self) -> float:
        return self.t

    def sleep(self, secs: float) -> None:
        self.t += secs

    advance = sleep  # alias for readability in tests


class ScriptedKeys:
    """read_key stand-in: returns queued keys one per call, then "" (no key).

    While the queue is empty, screens spin on their tick/sleep cycle with the
    fake clock advancing 0.25s per iteration — which is how timers (timed
    holds) elapse. A hung screen that never gets the key it needs is caught
    by max_reads instead of looping forever.

    Key names as run_screen expects them: "enter", "s", "b", "f", "p", "r",
    "j", "q", digits "0"-"9", "\\x7f" (backspace), "ctrl-z", plus the named
    special keys term.read_key produces: "up", "down", "left", "right", "esc".
    """

    def __init__(self, keys=(), max_reads: int = 100_000) -> None:
        self.queue: list[str] = list(keys)
        self.reads = 0
        self.max_reads = max_reads

    def push(self, *keys: str) -> None:
        self.queue.extend(keys)

    def __call__(self) -> str:
        self.reads += 1
        if self.reads > self.max_reads:
            raise RuntimeError(
                "ScriptedKeys: screen still running after "
                f"{self.max_reads} reads — missing a key in the script?"
            )
        return self.queue.pop(0) if self.queue else ""


# ---------------------------------------------------------------------------
# Player harness
# ---------------------------------------------------------------------------

@dataclass
class PlayerHarness:
    """A Player wired to a fake clock, scripted keys, and an off-screen Live."""

    player: Player
    cassette: Cassette
    clock: FakeClock
    keys: ScriptedKeys
    live: Live
    console: Console

    def run(self) -> None:
        """player.run() until the cassette completes (or keys demand BACK forever)."""
        self.player.run(self.live)

    def play_group(self, via_back: bool = False):
        """Play just the group under the playhead; returns the Event."""
        return self.player.play_group(self.live, via_back)


@pytest.fixture
def make_player(no_audio, isolated_state):
    """Factory: build a PlayerHarness from a cassette dict/Cassette + key script.

        harness = make_player(straight_cassette(), keys=["enter", "enter", ...])
        harness.run()
    """
    def _make(
        cassette,
        keys=(),
        *,
        cassette_path: str | None = None,
        start: float = 1_000_000.0,
    ) -> PlayerHarness:
        if isinstance(cassette, dict):
            cassette = load_cassette_from_dict(cassette)
        clock = FakeClock(start)
        scripted = ScriptedKeys(keys)
        console = Console(file=StringIO(), force_terminal=True, width=100, height=40)
        # Not started: render_layout's live.update() just stores the frame,
        # so nothing is written to the terminal and no refresh thread runs.
        live = Live(console=console)
        player = Player(
            cassette, cassette_path,
            now=clock.now, read_key=scripted, sleep=clock.sleep,
        )
        return PlayerHarness(
            player=player, cassette=cassette, clock=clock,
            keys=scripted, live=live, console=console,
        )
    return _make


# ---------------------------------------------------------------------------
# Cassette dict factories (dicts in the exact shape load_cassette_from_dict parses)
# ---------------------------------------------------------------------------

def _base_cassette(groups: list[dict], *, title: str, rest_default: int = 75) -> dict:
    return {
        "version": "1.1",
        "meta": {"date": "2026-07-12", "title": title, "rest_default": rest_default},
        "phases": [{"type": "main", "groups": groups}],
    }


@pytest.fixture
def straight_cassette():
    """Factory: single straight group.

    straight_cassette(name=..., rounds=3, reps=10, rest=60, load="24kg",
                      sets=[10, 10, 8], voice=True)
    `sets` (list of per-round targets) overrides `reps`; voice=True adds
    intro/round-complete/group-complete lines.
    """
    def _make(
        *,
        name: str = "Goblet Squat",
        rounds: int = 3,
        reps: int = 10,
        rest: int = 60,
        load: str = "24kg",
        sets: list[int] | None = None,
        voice: bool = False,
        title: str = "Straight Test",
    ) -> dict:
        ex: dict = {"name": name, "load": load, "reps": reps}
        if sets is not None:
            ex["sets"] = [{"reps": r} for r in sets]
        group: dict = {"type": "straight", "rounds": rounds, "rest": rest, "exercises": [ex]}
        if voice:
            group["voice_intro"] = f"{name}, {rounds} sets."
            group["voice_round_complete"] = [f"Round {i + 1} done." for i in range(rounds - 1)]
            group["voice_group_complete"] = f"{name} complete."
        return _base_cassette([group], title=title)
    return _make


@pytest.fixture
def superset_cassette():
    """Factory: one superset group of two (or more) rep exercises.

    superset_cassette(exercises=[("Bench", 8, "60kg"), ("Row", 10, "50kg")],
                      rounds=3, rest=90)
    Each exercise is (name, reps, load).
    """
    def _make(
        *,
        exercises: list[tuple[str, int, str]] | None = None,
        rounds: int = 3,
        rest: int = 90,
        voice: bool = False,
        title: str = "Superset Test",
    ) -> dict:
        if exercises is None:
            exercises = [("Bench Press", 8, "60kg"), ("Bent-over Row", 10, "50kg")]
        group: dict = {
            "type": "superset",
            "rounds": rounds,
            "rest": rest,
            "exercises": [
                {"name": n, "load": load, "reps": r} for n, r, load in exercises
            ],
        }
        if voice:
            group["voice_intro"] = "Superset time."
            group["voice_group_complete"] = "Superset complete."
        return _base_cassette([group], title=title)
    return _make


@pytest.fixture
def timed_cassette():
    """Factory: one straight group of a timed hold with per-round voice cues.

    timed_cassette(name="Plank", rounds=2, seconds=30,
                   cues=[[(10, "Halfway."), (25, "Five more.")], [(15, "Hold.")]])
    `cues` is one list of (at_seconds, line) per round -> voice_during_set.
    """
    default_cues = [
        [(10, "Halfway there."), (25, "Five seconds left.")],
        [(15, "Hold it strong.")],
    ]

    def _make(
        *,
        name: str = "Plank",
        rounds: int = 2,
        seconds: int = 30,
        rest: int = 45,
        cues: list[list[tuple[int, str]]] | None = None,
        title: str = "Timed Test",
    ) -> dict:
        if cues is None:
            cues = default_cues[:rounds]
        group = {
            "type": "straight",
            "rounds": rounds,
            "rest": rest,
            "exercises": [{"name": name, "timed": True, "reps": seconds}],
            "voice_during_set": [
                [{"at_seconds": at, "line": line} for at, line in round_cues]
                for round_cues in cues
            ],
        }
        return _base_cassette([group], title=title)
    return _make


# ---------------------------------------------------------------------------
# Checked-in cassette JSON fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fixture_cassette():
    """Load tests/fixtures/<name>.json as a parsed Cassette.

    fixture_cassette("straight") / ("superset") / ("timed_holds").
    fixture_cassette("straight", raw=True) returns the dict instead.
    """
    def _load(name: str, *, raw: bool = False):
        data = json.loads((FIXTURES_DIR / f"{name}.json").read_text())
        return data if raw else load_cassette_from_dict(data)
    return _load
