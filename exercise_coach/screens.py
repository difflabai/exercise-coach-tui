"""Interactive screens, all built on a single `run_screen` key/render loop.

Every screen is a render closure plus a key→Event map. `run_screen` owns the
cbreak/drain/render/read_key/sleep/restore cycle and the global keys:

- `p` shows the pause overlay (when a `pause` callable is supplied); the
  screen's pause closure receives the paused duration and compensates its
  own timers.
- Ctrl-Z raises `WorkoutPaused` so the CLI can suspend the process.

`now`, `read_key`, and `sleep` are injectable so screens can be driven
headlessly in tests; the defaults are the real clock/terminal.
"""

import time
from typing import Callable

from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from .audio import play_sound, sound_rest_done, stop_sounds
from .audio import settings as audio_settings
from .buddy import Mood
from .events import Event
from .jumpmenu import JumpMenuState, JumpTarget, render_jump_panel
from .models import Cassette, ExerciseData, Group, TimedCue
from .term import (
    WorkoutPaused,
    drain_stdin,
    enter_cbreak,
    restore_terminal,
)
from .term import (
    read_key as _real_read_key,
)
from .tts import say, say_sync, terminate_say
from .ui import (
    build_active_panel,
    build_overview,
    build_progress_bar,
    build_rest_panel,
    render_layout,
)

OVERTIME_NAGS = [
    "Rest is over, let's go.",
    "Time's up.",
    "Clock's done, you're not.",
    "Let's move.",
]


def get_cues_for_round(group: Group, round_idx: int) -> list[TimedCue]:
    if round_idx < len(group.voice_during_set):
        return group.voice_during_set[round_idx]
    return []


# ---------------------------------------------------------------------------
# The one key-input screen loop
# ---------------------------------------------------------------------------

def run_screen(
    live: Live,
    render_fn: Callable[[], None],
    keymap: dict[str, Event],
    tick: Callable[[], Event | None] | None = None,
    *,
    now: Callable[[], float] = time.time,
    read_key: Callable[[], str] | None = None,
    pause: Callable[[], None] | None = None,
    char_handler: Callable[[str], None] | None = None,
    poll: float = 0.25,
    sleep: Callable[[float], None] = time.sleep,
    suspendable: bool = True,
) -> Event:
    """Run one interactive screen until a mapped key (or tick) yields an Event.

    - `render_fn()` draws the current frame.
    - `keymap` maps keys (as returned by `read_key`) to Events.
    - `tick()` runs each cycle before rendering; returning an Event ends the
      screen (used by timers).
    - `pause` (if given) is invoked on 'p' with the terminal restored; it is
      expected to show the pause overlay and compensate the screen's timers.
    - `char_handler` receives unmapped non-empty keys (digit input etc.).
    - Ctrl-Z raises WorkoutPaused, unless `suspendable=False` (screens with
      pending unsaved input, like the failure-reps prompt, ignore it).

    When a custom `read_key` is injected (tests), the real terminal is left
    untouched: no cbreak, no stdin draining.
    """
    real_input = read_key is None
    if real_input:
        read_key = _real_read_key
        enter_cbreak()
        drain_stdin()
    try:
        while True:
            if tick is not None:
                ev = tick()
                if ev is not None:
                    return ev

            render_fn()

            key = read_key()
            if key in keymap:
                if real_input:
                    drain_stdin()
                return keymap[key]
            if key == "ctrl-z":
                if suspendable:
                    raise WorkoutPaused()
                sleep(poll)
                continue
            if key == "p" and pause is not None:
                if real_input:
                    restore_terminal()
                pause()
                if real_input:
                    enter_cbreak()
                    drain_stdin()
                continue
            # Global volume keys, available on every screen. A screen-specific
            # mapping always wins (keymap is checked first, above), and these
            # never count as activity: no timer state is touched — the loop
            # just re-renders with the new footer level.
            if key == "-":
                audio_settings.step_down()
                continue
            if key in ("+", "="):  # '=' is unshifted '+' on most layouts
                audio_settings.step_up()
                continue
            if key == "m":
                audio_settings.toggle_mute()
                if audio_settings.muted:
                    # Hard mute: silence in-flight speech and chimes too,
                    # not just the next utterance.
                    terminate_say()
                    stop_sounds()
                continue
            if key and char_handler is not None:
                char_handler(key)

            sleep(poll)
    finally:
        if real_input:
            restore_terminal()


# ---------------------------------------------------------------------------
# Pause
# ---------------------------------------------------------------------------

def pause_screen(
    live: Live, cassette: Cassette, cur_phase: int, cur_group: int,
    avg_rep_set: float = 30.0,
    *,
    now: Callable[[], float] = time.time,
    read_key: Callable[[], str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> float:
    """Show pause overlay. Returns seconds spent paused."""
    terminate_say()
    start = now()

    def render() -> None:
        overview = build_overview(cassette, cur_phase, cur_group)
        panel = Panel(
            Text("PAUSED", style="bold yellow", justify="center"),
            subtitle="p = resume  •  Ctrl-Z = suspend to shell",
            border_style="yellow", expand=True, padding=(2, 4),
        )
        render_layout(live, overview, panel, build_progress_bar(cassette, avg_rep_set),
                      mood=Mood.PAUSED, now=now())

    run_screen(
        live, render, {"p": Event.DONE, "enter": Event.DONE},
        now=now, read_key=read_key, sleep=sleep,
    )
    return now() - start


def _make_pause(
    live: Live, cassette: Cassette, cur_phase: int, cur_group: int,
    avg_rep_set: float, now, read_key, sleep,
    on_paused: Callable[[float], None] | None = None,
) -> Callable[[], None]:
    """Build a pause closure for run_screen; reports the paused duration."""
    def pause() -> None:
        secs = pause_screen(
            live, cassette, cur_phase, cur_group, avg_rep_set,
            now=now, read_key=read_key, sleep=sleep,
        )
        if on_paused is not None:
            on_paused(secs)
    return pause


# ---------------------------------------------------------------------------
# Jump menu overlay
# ---------------------------------------------------------------------------

def jump_menu_screen(
    live: Live, cassette: Cassette, cur_phase: int, cur_group: int,
    avg_rep_set: float = 30.0,
    *,
    now: Callable[[], float] = time.time,
    read_key: Callable[[], str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> JumpTarget | None:
    """Show the jump-menu overlay. Returns the chosen JumpTarget (group- or
    set-level, redo-flagged after a 'y' confirmation) or None on cancel.

    All menu logic lives in the pure JumpMenuState; this loop only feeds it
    keys via char_handler and renders. Rendering goes through render_layout
    (mood READY) so the global volume keys and footer stay live; 'p' pause is
    deliberately absent here (the menu itself is already a stopping point).
    """
    state = JumpMenuState(cassette, cur_phase, cur_group)
    result: dict = {"action": None}

    def tick() -> Event | None:
        # char_handler can't end run_screen directly; it parks the resolving
        # action here and the next tick ends the screen.
        return Event.DONE if result["action"] is not None else None

    def render() -> None:
        overview = build_overview(cassette, cur_phase, cur_group)
        render_layout(live, overview, render_jump_panel(state),
                      build_progress_bar(cassette, avg_rep_set),
                      mood=Mood.READY, now=now())

    def on_char(key: str) -> None:
        action = state.handle_key(key)
        if action is not None:
            result["action"] = action

    run_screen(
        live, render, {}, tick=tick,
        now=now, read_key=read_key, sleep=sleep,
        char_handler=on_char, poll=0.1,
    )
    kind, target = result["action"]
    return target if kind != "cancel" else None


# ---------------------------------------------------------------------------
# Transition (setup) screen between groups
# ---------------------------------------------------------------------------

def transition_screen(
    live: Live, cassette: Cassette, cur_phase: int, cur_group: int,
    group: Group, avg_rep_set: float = 30.0,
    *,
    completed: bool = False,
    now: Callable[[], float] = time.time,
    read_key: Callable[[], str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Event:
    """Show a setup/transition screen between groups. Blocks until Enter.
    Returns DONE, SKIP, or BACK.

    With `completed=True` (reached via 'b' onto an already-finished group)
    it instead offers an explicit redo: Enter/s = move on (DONE),
    r = REDO (the caller clears the group's progress), b = further BACK."""
    exercises_desc = []
    for ex in group.exercises:
        line = f"[bold]{ex.name}[/bold]"
        if ex.load:
            line += f"  ({ex.load})"
        exercises_desc.append(line)

    if completed:
        content = "[bold green]Already completed:[/bold green]\n" + "\n".join(exercises_desc)
        content += (
            "\n\n[dim]Enter = continue  •  r = redo (clears this group's progress)"
            "  •  b = back  •  j = jump[/dim]"
        )
        keymap = {
            "enter": Event.DONE, "s": Event.DONE, "r": Event.REDO,
            "b": Event.BACK, "j": Event.JUMP,
        }
        title = "Group complete"
        border = "green"
    else:
        content = "[bold cyan]Next up:[/bold cyan]\n" + "\n".join(exercises_desc)
        if group.setup:
            content += f"\n\n[yellow]{group.setup}[/yellow]"
        content += "\n\n[dim]Enter = ready  •  s = skip  •  b = back  •  j = jump[/dim]"
        keymap = {"enter": Event.DONE, "s": Event.SKIP, "b": Event.BACK, "j": Event.JUMP}
        title = "Setup"
        border = "cyan"

        # Voice the transition
        names = [ex.name for ex in group.exercises]
        voice_line = "Next up: " + " and ".join(names)
        if group.exercises and group.exercises[0].load:
            voice_line += f", {group.exercises[0].load}"
        say(voice_line)

    def render() -> None:
        overview = build_overview(cassette, cur_phase, cur_group)
        panel = Panel(
            content, title=title, border_style=border, expand=True, padding=(1, 4),
        )
        render_layout(live, overview, panel, build_progress_bar(cassette, avg_rep_set),
                      mood=Mood.READY, now=now())

    return run_screen(
        live, render, keymap,
        now=now, read_key=read_key, sleep=sleep,
        pause=_make_pause(live, cassette, cur_phase, cur_group, avg_rep_set,
                          now, read_key, sleep),
    )


# ---------------------------------------------------------------------------
# Context exercise screen
# ---------------------------------------------------------------------------

def context_screen(
    live: Live, cassette: Cassette, name: str, note: str,
    avg_rep_set: float = 30.0,
    *,
    now: Callable[[], float] = time.time,
    read_key: Callable[[], str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Event:
    """Show a context exercise note. Blocks until Enter."""
    def render() -> None:
        overview = build_overview(cassette, -1, -1)
        panel = Panel(
            f"[bold]{name}[/bold]\n{note}\n\nPress Enter to continue",
            title="Context", border_style="yellow", expand=True,
        )
        render_layout(live, overview, panel, build_progress_bar(cassette, avg_rep_set),
                      mood=Mood.READY, now=now())

    return run_screen(
        live, render, {"enter": Event.DONE},
        now=now, read_key=read_key, sleep=sleep,
        pause=_make_pause(live, cassette, -1, -1, avg_rep_set, now, read_key, sleep),
    )


# ---------------------------------------------------------------------------
# Rest timer
# ---------------------------------------------------------------------------

def rest_timer(
    live: Live, cassette: Cassette, cur_phase: int, cur_group: int,
    rest_seconds: int, avg_rep_set: float = 30.0,
    *,
    now: Callable[[], float] = time.time,
    read_key: Callable[[], str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Event:
    """Countdown rest timer. Returns DONE, SKIP, or BACK."""
    state = {"start": now(), "nags": 0, "dinged": False}

    def tick() -> Event | None:
        remaining = rest_seconds - (now() - state["start"])
        if remaining < 0:
            if not state["dinged"]:
                state["dinged"] = True
                play_sound(sound_rest_done())
            overtime_secs = int(-remaining)
            if overtime_secs >= 15 and overtime_secs // 15 > state["nags"]:
                state["nags"] = overtime_secs // 15
                say(OVERTIME_NAGS[state["nags"] % len(OVERTIME_NAGS)])
        return None

    def render() -> None:
        remaining = rest_seconds - (now() - state["start"])
        overview = build_overview(cassette, cur_phase, cur_group)
        panel = build_rest_panel(rest_seconds, remaining, remaining < 0)
        if remaining < 0:
            mood, mood_intensity = Mood.OVERTIME, float(state["nags"])
        else:
            mood, mood_intensity = Mood.RESTING, remaining  # seconds left: wakes Rep near 0
        # The in-progress rest belongs to the round already recorded, so the
        # cassette can't see it — pass the live countdown into the ETA.
        progress = build_progress_bar(cassette, avg_rep_set, max(0.0, remaining))
        render_layout(live, overview, panel, progress,
                      mood=mood, mood_intensity=mood_intensity, now=now())

    def on_paused(secs: float) -> None:
        state["start"] += secs

    return run_screen(
        live, render,
        {"enter": Event.DONE, "s": Event.SKIP, "b": Event.BACK, "j": Event.JUMP},
        tick=tick, now=now, read_key=read_key, sleep=sleep,
        pause=_make_pause(live, cassette, cur_phase, cur_group, avg_rep_set,
                          now, read_key, sleep, on_paused),
    )


# ---------------------------------------------------------------------------
# Timed hold
# ---------------------------------------------------------------------------

def timed_hold(
    live: Live, cassette: Cassette, cur_phase: int, cur_group: int,
    group: Group, ex: ExerciseData, round_idx: int, ex_idx: int,
    avg_rep_set: float = 30.0,
    *,
    now: Callable[[], float] = time.time,
    read_key: Callable[[], str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[Event, int]:
    """Run a timed hold. Returns (event, seconds held) — mirroring
    rep_set_screen's tuple return so the caller can record the actual time.

    - The timer running out returns (DONE, duration): a full hold.
    - Enter ends the hold early, crediting the second in progress:
      seconds held = int(elapsed) + 1, capped at the target — so ending
      during the final displayed second rounds up to the full duration.
      The caller records a shortfall (held < duration) as a failure.
    - 'f' ("I broke earlier") returns (FAIL, 0); the caller prompts for the
      actual seconds via get_failure_reps.
    - SKIP/BACK return 0 seconds; nothing is recorded.
    """
    duration = ex.sets[round_idx].reps
    cues = get_cues_for_round(group, round_idx)

    # Get in position — a real run_screen (not a blind sleep loop) so keys
    # work here like on every other screen: m/-/+ volume, p pause, Ctrl-Z,
    # and the hold's own s/b (skip/back apply to the whole hold).
    say_sync("Get in position")
    countdown = {"start": now()}

    def countdown_tick() -> Event | None:
        return Event.DONE if now() - countdown["start"] >= 3 else None

    def countdown_render() -> None:
        secs_left = max(1, 3 - int(now() - countdown["start"]))
        overview = build_overview(cassette, cur_phase, cur_group)
        panel = build_active_panel(
            cassette, group, ex, round_idx, ex_idx,
            status="Get in position...", timer_text=str(secs_left), timer_style="bold yellow",
        )
        render_layout(live, overview, panel, build_progress_bar(cassette, avg_rep_set),
                      mood=Mood.READY, now=now())

    def countdown_on_paused(secs: float) -> None:
        countdown["start"] += secs

    ev = run_screen(
        live, countdown_render, {"s": Event.SKIP, "b": Event.BACK},
        tick=countdown_tick, now=now, read_key=read_key, sleep=sleep,
        pause=_make_pause(live, cassette, cur_phase, cur_group, avg_rep_set,
                          now, read_key, sleep, countdown_on_paused),
    )
    if ev is not Event.DONE:
        return ev, 0

    say("Go")
    state = {"start": now(), "cue_idx": 0, "remaining": float(duration)}
    key_hint = (
        "Enter = done early  •  f = broke earlier  •  s = skip  •  b = back"
        "  •  p = pause  •  m/-/+ vol"
    )

    def tick() -> Event | None:
        elapsed = now() - state["start"]
        state["remaining"] = duration - elapsed
        if state["remaining"] <= 0:
            return Event.DONE
        # Fire cues at their timestamps
        if state["cue_idx"] < len(cues) and elapsed >= cues[state["cue_idx"]].at_seconds:
            say(cues[state["cue_idx"]].line)
            state["cue_idx"] += 1
        return None

    def render() -> None:
        secs_left = int(state["remaining"]) + 1
        overview = build_overview(cassette, cur_phase, cur_group)
        panel = build_active_panel(
            cassette, group, ex, round_idx, ex_idx,
            status=f"HOLD!\n[dim]{key_hint}[/dim]",
            timer_text=f"{secs_left}s", timer_style="bold green",
        )
        # Fraction of the hold elapsed — Rep trembles harder as it approaches 1.
        elapsed_frac = min(1.0, max(0.0, 1 - state["remaining"] / duration)) if duration else 1.0
        render_layout(live, overview, panel, build_progress_bar(cassette, avg_rep_set),
                      mood=Mood.HOLDING, mood_intensity=elapsed_frac, now=now())

    def on_paused(secs: float) -> None:
        state["start"] += secs

    # 'j' stays unmapped mid-hold (deliberate): unlike a rep set there is no
    # way to abandon a hold into the jump menu without losing the clock.
    ev = run_screen(
        live, render,
        {"enter": Event.DONE, "f": Event.FAIL, "s": Event.SKIP, "b": Event.BACK},
        tick=tick, now=now, read_key=read_key, sleep=sleep,
        pause=_make_pause(live, cassette, cur_phase, cur_group, avg_rep_set,
                          now, read_key, sleep, on_paused),
    )
    if ev is not Event.DONE:
        return ev, 0
    # Credit the second in progress (documented boundary above): the timer
    # running out lands at elapsed >= duration and caps to a full hold.
    held = min(duration, int(now() - state["start"]) + 1)
    if held >= duration:
        say("Done")
    return ev, held


# ---------------------------------------------------------------------------
# Rep-based set
# ---------------------------------------------------------------------------

def rep_set_screen(
    live: Live, cassette: Cassette, cur_phase: int, cur_group: int,
    group: Group, ex: ExerciseData, round_idx: int, ex_idx: int,
    avg_rep_set: float = 30.0,
    *,
    now: Callable[[], float] = time.time,
    read_key: Callable[[], str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[Event, float]:
    """Wait for the user to finish a rep-based set.
    Returns (event, total seconds spent paused) so the caller can measure
    active set duration. Event is DONE, FAIL, SKIP, or BACK."""
    key_hint = "Enter = done  •  f = failed  •  s = skip  •  b = back  •  j = jump  •  p = pause  •  m/-/+ vol"
    paused = {"secs": 0.0}

    def render() -> None:
        overview = build_overview(cassette, cur_phase, cur_group)
        panel = build_active_panel(
            cassette, group, ex, round_idx, ex_idx, status=key_hint,
        )
        render_layout(live, overview, panel, build_progress_bar(cassette, avg_rep_set),
                      mood=Mood.WORKING, now=now())

    def on_paused(secs: float) -> None:
        paused["secs"] += secs

    ev = run_screen(
        live, render,
        {"enter": Event.DONE, "f": Event.FAIL, "s": Event.SKIP,
         "b": Event.BACK, "j": Event.JUMP},
        now=now, read_key=read_key, sleep=sleep,
        pause=_make_pause(live, cassette, cur_phase, cur_group, avg_rep_set,
                          now, read_key, sleep, on_paused),
    )
    return ev, paused["secs"]


# ---------------------------------------------------------------------------
# Failure input flow
# ---------------------------------------------------------------------------

def get_failure_reps(
    live: Live, cassette: Cassette, cur_phase: int, cur_group: int,
    group: Group, ex: ExerciseData, round_idx: int, ex_idx: int,
    target_reps: int, avg_rep_set: float = 30.0,
    *,
    now: Callable[[], float] = time.time,
    read_key: Callable[[], str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Prompt for actual reps after failure — or, for a timed hold, the
    actual seconds held ("I broke earlier"). Returns the count clamped to
    the target (`target_reps` is the hold duration for timed exercises)."""
    state = {"digits": ""}
    prompt = "Seconds held" if ex.timed else "Reps completed"

    def render() -> None:
        display_reps = state["digits"] if state["digits"] else "_"
        overview = build_overview(cassette, cur_phase, cur_group)
        panel = build_active_panel(
            cassette, group, ex, round_idx, ex_idx,
            status=f"{prompt}: {display_reps}",
            timer_text="Type number, then Enter", timer_style="bold yellow",
        )
        render_layout(live, overview, panel, build_progress_bar(cassette, avg_rep_set),
                      mood=Mood.WORKING, now=now())

    def on_char(key: str) -> None:
        # ASCII only: isdigit() also accepts '²' etc., which the int() below
        # would crash on — non-ASCII digits are simply ignored.
        if key.isascii() and key.isdigit():
            state["digits"] += key
        elif key == "\x7f" and state["digits"]:  # backspace
            state["digits"] = state["digits"][:-1]

    # suspendable=False: Ctrl-Z would discard the typed rep count (it is only
    # written back by the player after this returns), so ignore it here —
    # matching the original app's behavior on this screen.
    run_screen(
        live, render, {"enter": Event.DONE},
        now=now, read_key=read_key, sleep=sleep,
        char_handler=on_char, poll=0.1, suspendable=False,
    )

    actual = int(state["digits"]) if state["digits"] else 0
    return min(actual, target_reps)
