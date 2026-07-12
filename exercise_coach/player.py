"""Main playback loop and its interactive screens."""

import time

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from .audio import play_sound, sound_group_complete, sound_rest_done, sound_set_complete
from .cassette import count_sets, rounds_completed
from .models import Cassette, ExerciseData, Group, TimedCue
from .state import clear_state, print_log, save_log, save_state
from .term import WorkoutPaused, drain_stdin, enter_cbreak, read_key, restore_terminal
from .tts import say, say_sync, speak, terminate_say
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


# ---------------------------------------------------------------------------
# Cassette helpers (player-side, mutating or voice-coupled)
# ---------------------------------------------------------------------------

def clear_group_progress(group: Group) -> None:
    """Reset all completed sets in a group so it can be replayed."""
    group.skipped = False
    for ex in group.exercises:
        for s in ex.sets:
            s.actual_reps = None
            s.failure = False


def go_back_to_previous_group(
    cassette: Cassette, cur_pi: int, cur_gi: int,
) -> tuple[int, int] | None:
    """Go back to the previous group (opposite of skip).
    Clears progress in both the current and target groups.
    Returns (phase_idx, group_idx) of the target group, or None if at the start."""
    # Build flat list of (phase_idx, group_idx) pairs
    all_groups = []
    for pi, phase in enumerate(cassette.phases):
        for gi in range(len(phase.groups)):
            all_groups.append((pi, gi))

    # Find current position in the flat list
    try:
        pos = all_groups.index((cur_pi, cur_gi))
    except ValueError:
        return None

    if pos == 0:
        return None  # already at the very first group

    target_pi, target_gi = all_groups[pos - 1]

    # Clear progress in current group
    clear_group_progress(cassette.phases[cur_pi].groups[cur_gi])
    # Clear progress in target group so it replays
    clear_group_progress(cassette.phases[target_pi].groups[target_gi])

    return (target_pi, target_gi)


def get_cues_for_round(group: Group, round_idx: int) -> list[TimedCue]:
    if round_idx < len(group.voice_during_set):
        return group.voice_during_set[round_idx]
    return []


def speak_round_complete(group: Group, round_idx: int) -> None:
    if round_idx < len(group.voice_round_complete):
        speak(group.voice_round_complete[round_idx])


# ---------------------------------------------------------------------------
# Pause
# ---------------------------------------------------------------------------

def pause_screen(
    live: Live, cassette: Cassette, cur_phase: int, cur_group: int,
    avg_rep_set: float = 30.0,
) -> float:
    """Show pause overlay. Returns seconds spent paused."""
    terminate_say()

    start = time.time()
    enter_cbreak()
    drain_stdin()

    try:
        while True:
            overview = build_overview(cassette, cur_phase, cur_group)
            panel = Panel(
                Text("PAUSED", style="bold yellow", justify="center"),
                subtitle="p = resume  •  Ctrl-Z = suspend to shell",
                border_style="yellow", expand=True, padding=(2, 4),
            )
            progress_text = build_progress_bar(cassette, avg_rep_set)
            render_layout(live, overview, panel, progress_text)

            key = read_key()
            if key in ("p", "enter"):
                drain_stdin()
                break
            if key == "ctrl-z":
                raise WorkoutPaused()

            time.sleep(0.25)
    finally:
        restore_terminal()

    return time.time() - start


def transition_screen(
    live: Live, cassette: Cassette, cur_phase: int, cur_group: int,
    group: Group, avg_rep_set: float = 30.0,
) -> str:
    """Show a setup/transition screen between groups. Blocks until Enter.
    Returns 'go_back' if b pressed, 'done' otherwise."""
    # Build description of what's next
    exercises_desc = []
    for ex in group.exercises:
        line = f"[bold]{ex.name}[/bold]"
        if ex.load:
            line += f"  ({ex.load})"
        exercises_desc.append(line)

    content = "[bold cyan]Next up:[/bold cyan]\n" + "\n".join(exercises_desc)
    if group.setup:
        content += f"\n\n[yellow]{group.setup}[/yellow]"
    content += "\n\n[dim]Enter = ready  •  s = skip  •  b = back[/dim]"

    # Voice the transition
    names = [ex.name for ex in group.exercises]
    voice_line = "Next up: " + " and ".join(names)
    if group.exercises and group.exercises[0].load:
        voice_line += f", {group.exercises[0].load}"
    say(voice_line)

    enter_cbreak()
    drain_stdin()

    try:
        while True:
            overview = build_overview(cassette, cur_phase, cur_group)
            panel = Panel(
                content, title="Setup", border_style="cyan", expand=True, padding=(1, 4),
            )
            progress_text = build_progress_bar(cassette, avg_rep_set)
            render_layout(live, overview, panel, progress_text)

            key = read_key()
            if key == "enter":
                break
            elif key == "p":
                restore_terminal()
                pause_screen(live, cassette, cur_phase, cur_group, avg_rep_set)
                enter_cbreak()
                drain_stdin()
                continue
            elif key == "s":
                group.skipped = True
                break
            elif key == "b":
                drain_stdin()
                restore_terminal()
                return "go_back"
            elif key == "ctrl-z":
                raise WorkoutPaused()

            time.sleep(0.25)
    finally:
        restore_terminal()

    return "done"


# ---------------------------------------------------------------------------
# Rest timer
# ---------------------------------------------------------------------------

def rest_timer(
    cassette: Cassette, cur_phase: int, cur_group: int,
    rest_seconds: int, live: Live, avg_rep_set: float = 30.0,
) -> str:
    """Countdown rest timer. Returns 'skip_group' if s pressed, else 'done'."""
    start = time.time()
    nag_count = 0
    rest_done_dinged = False

    enter_cbreak()
    drain_stdin()

    try:
        while True:
            elapsed = time.time() - start
            remaining = rest_seconds - elapsed
            overtime = remaining < 0

            if overtime:
                if not rest_done_dinged:
                    rest_done_dinged = True
                    play_sound(sound_rest_done())
                overtime_secs = int(-remaining)
                if overtime_secs >= 15 and overtime_secs // 15 > nag_count:
                    nag_count = overtime_secs // 15
                    say(OVERTIME_NAGS[nag_count % len(OVERTIME_NAGS)])

            overview = build_overview(cassette, cur_phase, cur_group)
            panel = build_rest_panel(rest_seconds, remaining, overtime)
            progress_text = build_progress_bar(cassette, avg_rep_set)
            render_layout(live, overview, panel, progress_text)

            key = read_key()
            if key == "enter":
                break
            elif key == "s":
                drain_stdin()
                return "skip_group"
            elif key == "b":
                drain_stdin()
                return "go_back"
            elif key == "p":
                restore_terminal()
                paused = pause_screen(live, cassette, cur_phase, cur_group, avg_rep_set)
                start += paused
                enter_cbreak()
                drain_stdin()
                continue
            elif key == "ctrl-z":
                raise WorkoutPaused()

            time.sleep(0.25)
    finally:
        restore_terminal()

    return "done"


# ---------------------------------------------------------------------------
# Timed hold
# ---------------------------------------------------------------------------

def timed_hold(
    cassette: Cassette, cur_phase: int, cur_group: int,
    group: Group, ex: ExerciseData, round_idx: int, ex_idx: int,
    live: Live, avg_rep_set: float = 30.0,
) -> str:
    """Run a timed hold. Returns 'skip_group' if s pressed, else 'done'."""
    duration = ex.sets[round_idx].reps
    cues = get_cues_for_round(group, round_idx)

    # Get in position
    say_sync("Get in position")
    for countdown in range(3, 0, -1):
        overview = build_overview(cassette, cur_phase, cur_group)
        panel = build_active_panel(
            cassette, group, ex, round_idx, ex_idx,
            status="Get in position...", timer_text=str(countdown), timer_style="bold yellow",
        )
        progress_text = build_progress_bar(cassette, avg_rep_set)
        render_layout(live, overview, panel, progress_text)
        time.sleep(1)

    say("Go")

    cue_idx = 0
    start = time.time()

    enter_cbreak()
    drain_stdin()

    try:
        while True:
            elapsed = time.time() - start
            remaining = duration - elapsed
            if remaining <= 0:
                break

            # Fire cues at their timestamps
            if cue_idx < len(cues) and elapsed >= cues[cue_idx].at_seconds:
                say(cues[cue_idx].line)
                cue_idx += 1

            secs_left = int(remaining) + 1
            overview = build_overview(cassette, cur_phase, cur_group)
            panel = build_active_panel(
                cassette, group, ex, round_idx, ex_idx,
                status="HOLD!", timer_text=f"{secs_left}s", timer_style="bold green",
            )
            progress_text = build_progress_bar(cassette, avg_rep_set)
            render_layout(live, overview, panel, progress_text)

            key = read_key()
            if key == "s":
                drain_stdin()
                restore_terminal()
                return "skip_group"
            elif key == "b":
                drain_stdin()
                restore_terminal()
                return "go_back"
            elif key == "p":
                restore_terminal()
                paused = pause_screen(live, cassette, cur_phase, cur_group, avg_rep_set)
                start += paused
                enter_cbreak()
                drain_stdin()
                continue
            elif key == "ctrl-z":
                raise WorkoutPaused()

            time.sleep(0.25)
    finally:
        restore_terminal()

    say("Done")
    return "done"


# ---------------------------------------------------------------------------
# Failure input flow
# ---------------------------------------------------------------------------

def get_failure_reps(
    cassette: Cassette, cur_phase: int, cur_group: int,
    group: Group, ex: ExerciseData, round_idx: int, ex_idx: int,
    target_reps: int, live: Live, avg_rep_set: float = 30.0,
) -> int:
    """Prompt for actual reps after failure. Returns clamped rep count."""
    digits = ""
    enter_cbreak()
    drain_stdin()
    try:
        while True:
            display_reps = digits if digits else "_"
            overview = build_overview(cassette, cur_phase, cur_group)
            panel = build_active_panel(
                cassette, group, ex, round_idx, ex_idx,
                status=f"Reps completed: {display_reps}",
                timer_text="Type number, then Enter", timer_style="bold yellow",
            )
            progress_text = build_progress_bar(cassette, avg_rep_set)
            render_layout(live, overview, panel, progress_text)

            key = read_key()
            if key == "enter":
                break
            elif key and key.isdigit():
                digits += key
            elif key == "\x7f" and digits:  # backspace
                digits = digits[:-1]

            time.sleep(0.1)
    finally:
        restore_terminal()

    actual = int(digits) if digits else 0
    return min(actual, target_reps)


# ---------------------------------------------------------------------------
# Main playback loop
# ---------------------------------------------------------------------------

def play_cassette(cassette: Cassette, cassette_path: str | None = None) -> None:
    """Play a cassette from start to finish (or from resume position)."""
    console = Console()

    # Check if already complete
    total, done = count_sets(cassette)
    if total > 0 and done >= total:
        console.print("[green]All exercises already complete![/green]")
        print_log(cassette)
        save_log(cassette)
        return

    rep_set_durations: list[float] = []

    def avg_rep_set() -> float:
        return sum(rep_set_durations) / len(rep_set_durations) if rep_set_durations else 30.0

    speak(cassette.voice_session_intro)

    with Live(console=console, refresh_per_second=4, screen=True) as live:
        # Context exercises
        if cassette.context_exercises:
            for ctx in cassette.context_exercises:
                speak(ctx.voice)
                overview = build_overview(cassette, -1, -1)
                ctx_panel = Panel(
                    f"[bold]{ctx.name}[/bold]\n{ctx.note}\n\nPress Enter to continue",
                    title="Context", border_style="yellow", expand=True,
                )
                progress_text = build_progress_bar(cassette, avg_rep_set())
                render_layout(live, overview, ctx_panel, progress_text)
                enter_cbreak()
                drain_stdin()
                try:
                    while True:
                        key = read_key()
                        if key == "enter":
                            break
                        elif key == "p":
                            restore_terminal()
                            pause_screen(live, cassette, -1, -1, avg_rep_set())
                            enter_cbreak()
                            drain_stdin()
                            continue
                        elif key == "ctrl-z":
                            raise WorkoutPaused()
                        time.sleep(0.25)
                finally:
                    restore_terminal()

        # Walk phases → groups → rounds → exercises
        pi = 0
        start_gi = 0
        resuming = False
        while pi < len(cassette.phases):
            phase = cassette.phases[pi]
            if not resuming:
                speak(phase.voice_intro)

            gi = start_gi
            start_gi = 0
            jump_back = None

            while gi < len(phase.groups):
                group = phase.groups[gi]
                if group.skipped:
                    gi += 1
                    continue

                # Transition screen between groups (not before the first unstarted group)
                already_started = rounds_completed(group) > 0
                if not already_started and not resuming:
                    tr_result = transition_screen(live, cassette, pi, gi, group, avg_rep_set())
                    if tr_result == "go_back":
                        back_result = go_back_to_previous_group(cassette, pi, gi)
                        if back_result is not None:
                            jump_back = back_result
                            break
                        continue
                    if group.skipped:
                        gi += 1
                        continue

                if not resuming:
                    speak(group.voice_intro)
                resuming = False

                skip_group = False
                round_idx = rounds_completed(group)

                while round_idx < group.rounds:
                    if skip_group:
                        break

                    for ei, ex in enumerate(group.exercises):
                        if skip_group or jump_back is not None:
                            break

                        set_data = ex.sets[round_idx]
                        if set_data.actual_reps is not None:
                            continue  # already done (resume)

                        if ex.timed:
                            result = timed_hold(
                                cassette, pi, gi, group, ex, round_idx, ei,
                                live, avg_rep_set(),
                            )
                            if result == "skip_group":
                                skip_group = True
                                break
                            if result == "go_back":
                                back_result = go_back_to_previous_group(cassette, pi, gi)
                                if back_result is not None:
                                    jump_back = back_result
                                break
                            set_data.actual_reps = set_data.reps
                        else:
                            # Rep-based: show panel, wait for key
                            set_start = time.time()
                            key_hint = "Enter = done  •  f = failed  •  s = skip  •  b = back  •  p = pause"
                            enter_cbreak()
                            drain_stdin()
                            try:
                                while True:
                                    overview = build_overview(cassette, pi, gi)
                                    panel = build_active_panel(
                                        cassette, group, ex, round_idx, ei,
                                        status=key_hint,
                                    )
                                    progress_text = build_progress_bar(cassette, avg_rep_set())
                                    render_layout(live, overview, panel, progress_text)

                                    key = read_key()
                                    if key == "enter":
                                        set_data.actual_reps = set_data.reps
                                        break
                                    elif key == "f":
                                        restore_terminal()
                                        actual = get_failure_reps(
                                            cassette, pi, gi, group, ex,
                                            round_idx, ei, set_data.reps,
                                            live, avg_rep_set(),
                                        )
                                        set_data.actual_reps = actual
                                        set_data.failure = True
                                        enter_cbreak()
                                        break
                                    elif key == "s":
                                        drain_stdin()
                                        skip_group = True
                                        break
                                    elif key == "b":
                                        back_result = go_back_to_previous_group(cassette, pi, gi)
                                        if back_result is not None:
                                            jump_back = back_result
                                        break
                                    elif key == "p":
                                        restore_terminal()
                                        paused = pause_screen(live, cassette, pi, gi, avg_rep_set())
                                        set_start += paused
                                        enter_cbreak()
                                        drain_stdin()
                                        continue
                                    elif key == "ctrl-z":
                                        raise WorkoutPaused()

                                    time.sleep(0.25)
                            finally:
                                restore_terminal()

                            if not skip_group and jump_back is None:
                                rep_set_durations.append(time.time() - set_start)

                        if skip_group or jump_back is not None:
                            break

                        # Set complete
                        play_sound(sound_set_complete())

                    if jump_back is not None:
                        break

                    if skip_group:
                        group.skipped = True
                        say(f"Skipping {group.exercises[0].name}")
                        save_state(cassette, {"phase_idx": pi, "group_idx": gi, "round_idx": round_idx}, cassette_path)
                        break

                    # Round complete
                    speak_round_complete(group, round_idx)
                    play_sound(sound_set_complete())
                    save_state(cassette, {"phase_idx": pi, "group_idx": gi, "round_idx": round_idx + 1}, cassette_path)

                    # Rest (skip after last round of last group of last phase)
                    if round_idx < group.rounds - 1:
                        result = rest_timer(
                            cassette, pi, gi, group.rest, live, avg_rep_set(),
                        )
                        if result == "skip_group":
                            group.skipped = True
                            say(f"Skipping {group.exercises[0].name}")
                            save_state(cassette, {"phase_idx": pi, "group_idx": gi, "round_idx": round_idx + 1}, cassette_path)
                            break
                        if result == "go_back":
                            back_result = go_back_to_previous_group(cassette, pi, gi)
                            if back_result is not None:
                                jump_back = back_result
                                break
                            continue

                    round_idx += 1

                if jump_back is not None:
                    break

                # Group complete
                if not group.skipped:
                    speak(group.voice_group_complete)
                    play_sound(sound_group_complete())

                gi += 1

            if jump_back is not None:
                target_pi, target_gi = jump_back
                pi = target_pi
                start_gi = target_gi
                resuming = True
                continue

            pi += 1

    speak(cassette.voice_session_complete)
    console.print("[bold green]Workout complete![/bold green]\n")
    print_log(cassette)
    save_log(cassette)
    clear_state()
