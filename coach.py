#!/usr/bin/env python3
"""Workout Coach TUI — interactive terminal workout companion with voice coaching."""

import argparse
import json
import os
import random
import re
import select
import subprocess
import sys
import termios
import time
import tty
from dataclasses import dataclass, field, asdict
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TextColumn
from rich.layout import Layout
from rich.text import Text
from rich import box

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

STATE_FILE = Path(__file__).parent / ".workout_state.json"
STALE_HOURS = 4


@dataclass
class Exercise:
    name: str
    total_sets: int
    reps: int  # reps count, or seconds if timed
    timed: bool  # True if reps actually means seconds (e.g. 40s)
    weight: str  # "BW", "55 lbs", etc.
    phase: str  # ignored in output
    completed_sets: int = 0

    @property
    def done(self) -> bool:
        return self.completed_sets >= self.total_sets

    def log_str(self) -> str:
        reps_str = f"{self.reps}s" if self.timed else str(self.reps)
        weight_part = f" | {self.weight}" if self.weight else ""
        return f"{self.name:<25} {self.total_sets}[{self.completed_sets}]×{reps_str}{weight_part}"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

SET_RE = re.compile(
    r"(\d+)"           # total sets
    r"(?:\[(\d+)\])?"  # optional [completed]
    r"[×x]"            # separator
    r"(\d+)(s)?"       # reps or seconds
)


def parse_exercise(line: str) -> Exercise | None:
    line = line.strip()
    if not line:
        return None
    parts = [p.strip() for p in line.split("|")]
    if not parts:
        return None

    first = parts[0]
    m = SET_RE.search(first)
    if not m:
        return None

    name = first[: m.start()].strip()
    total_sets = int(m.group(1))
    completed = int(m.group(2)) if m.group(2) else 0
    reps = int(m.group(3))
    timed = m.group(4) == "s"

    weight = parts[1].strip() if len(parts) > 1 else ""
    phase = parts[2].strip() if len(parts) > 2 else ""

    return Exercise(
        name=name,
        total_sets=total_sets,
        reps=reps,
        timed=timed,
        weight=weight,
        phase=phase,
        completed_sets=completed,
    )


def parse_workout(text: str) -> list[Exercise]:
    exercises = []
    for line in text.splitlines():
        ex = parse_exercise(line)
        if ex:
            exercises.append(ex)
    return exercises


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def save_state(exercises: list[Exercise]) -> None:
    data = {
        "timestamp": time.time(),
        "exercises": [asdict(e) for e in exercises],
    }
    STATE_FILE.write_text(json.dumps(data, indent=2))


def load_state() -> tuple[list[Exercise], float] | None:
    if not STATE_FILE.exists():
        return None
    try:
        data = json.loads(STATE_FILE.read_text())
        exercises = [Exercise(**e) for e in data["exercises"]]
        return exercises, data["timestamp"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def clear_state() -> None:
    if STATE_FILE.exists():
        STATE_FILE.unlink()


def exercises_match(a: list[Exercise], b: list[Exercise]) -> bool:
    """Check if two exercise lists describe the same workout (ignoring progress)."""
    if len(a) != len(b):
        return False
    for x, y in zip(a, b):
        if (x.name, x.total_sets, x.reps, x.timed) != (y.name, y.total_sets, y.reps, y.timed):
            return False
    return True


# ---------------------------------------------------------------------------
# Voice coaching (non-blocking macOS `say`)
# ---------------------------------------------------------------------------

HOLD_MESSAGES = [
    "You're doing great, keep holding!",
    "Stay strong, don't give up!",
    "Breathe through it!",
    "Remember to breathe!",
    "Deep breaths into the stomach!",
    "Almost there, keep pushing!",
    "You're tougher than you think!",
    "Hold it, hold it, hold it!",
    "This is where champions are made!",
    "Pain is temporary, pride is forever!",
    "Squeeze harder, let's go!",
    "You've got this, stay tight!",
    "Mind over matter, keep going!",
    "Every second counts, stay in it!",
    "Don't quit on me now!",
    "That's it, right there, perfect form!",
    "Embrace the burn!",
]

REST_MESSAGES = [
    "Nice work on that set!",
    "Shake it out, you earned this rest.",
    "Great effort, keep it up!",
    "Solid set, stay focused.",
    "You're crushing it today!",
    "Way to push through!",
    "That looked strong!",
    "Recover and reload.",
    "One step closer to the finish!",
    "Enjoy the break, next set's gonna be even better.",
    "You're making progress, keep showing up!",
    "Take a breath, you've earned it.",
    "Beast mode activated!",
    "Respect the rest, then attack the next set.",
    "Looking good, keep that energy!",
]

_say_proc: subprocess.Popen | None = None


def _generate_tone(frequency: int, duration_ms: int, volume: float = 0.5) -> bytes:
    """Generate a WAV tone in memory. Returns raw WAV bytes."""
    import struct
    import wave
    import io
    sample_rate = 44100
    n_samples = int(sample_rate * duration_ms / 1000)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        import math
        frames = b"".join(
            struct.pack("<h", int(volume * 32767 * math.sin(2 * math.pi * frequency * i / sample_rate)))
            for i in range(n_samples)
        )
        wf.writeframes(frames)
    return buf.getvalue()


def _generate_reward_tone() -> bytes:
    """A short rising two-note chime for set completion."""
    import io
    tone1 = _generate_tone(880, 120, 0.4)   # A5
    tone2 = _generate_tone(1175, 200, 0.4)  # D6
    # Concatenate by appending raw audio data from tone2 onto tone1
    import wave
    buf = io.BytesIO()
    with wave.open(io.BytesIO(tone1), "rb") as w1, wave.open(io.BytesIO(tone2), "rb") as w2:
        with wave.open(buf, "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(44100)
            out.writeframes(w1.readframes(w1.getnframes()))
            out.writeframes(w2.readframes(w2.getnframes()))
    return buf.getvalue()


def _generate_exercise_complete_tone() -> bytes:
    """A three-note ascending chime for exercise completion."""
    import io
    tones = [
        _generate_tone(784, 100, 0.4),   # G5
        _generate_tone(988, 100, 0.4),   # B5
        _generate_tone(1319, 250, 0.4),  # E6
    ]
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(44100)
        for t in tones:
            with wave.open(io.BytesIO(t), "rb") as w:
                out.writeframes(w.readframes(w.getnframes()))
    return buf.getvalue()


# Pre-generate sounds at import time
_SOUND_SET_COMPLETE = _generate_reward_tone()
_SOUND_EXERCISE_COMPLETE = _generate_exercise_complete_tone()


def play_sound(sound_data: bytes) -> None:
    """Play a WAV sound from bytes (non-blocking). Uses afplay on macOS, aplay on Linux."""
    import tempfile
    try:
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.write(sound_data)
        tmp.close()
        # Try afplay (macOS), then aplay (Linux)
        for cmd in (["afplay", tmp.name], ["aplay", "-q", tmp.name]):
            try:
                subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                return
            except FileNotFoundError:
                continue
    except OSError:
        pass


def say(text: str) -> None:
    global _say_proc
    try:
        if _say_proc and _say_proc.poll() is None:
            _say_proc.terminate()
        _say_proc = subprocess.Popen(
            ["say", text],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        pass  # not on macOS


def say_sync(text: str, wait: float = 0) -> None:
    """Say something and optionally wait after it finishes."""
    global _say_proc
    try:
        if _say_proc and _say_proc.poll() is None:
            _say_proc.terminate()
        _say_proc = subprocess.Popen(
            ["say", text],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _say_proc.wait()
        if wait > 0:
            time.sleep(wait)
    except FileNotFoundError:
        if wait > 0:
            time.sleep(wait)


# ---------------------------------------------------------------------------
# Log output
# ---------------------------------------------------------------------------

def format_log(exercises: list[Exercise]) -> str:
    lines = []
    for ex in exercises:
        lines.append(ex.log_str())
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Terminal helpers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# TUI rendering
# ---------------------------------------------------------------------------

def build_overview(exercises: list[Exercise], current_idx: int) -> Table:
    table = Table(
        title="Workout",
        box=box.SIMPLE_HEAVY,
        show_header=False,
        pad_edge=False,
        expand=True,
    )
    table.add_column("Exercise", ratio=1)
    for i, ex in enumerate(exercises):
        text = Text(ex.log_str())
        if ex.done:
            text.stylize("dim green")
        elif i == current_idx:
            text.stylize("bold white on blue")
        table.add_row(text)
    return table


def build_active_panel(
    ex: Exercise,
    set_num: int,
    status: str = "",
    timer_text: str = "",
    timer_style: str = "bold white",
) -> Panel:
    lines: list[str] = []
    reps_label = f"{ex.reps}s hold" if ex.timed else f"{ex.reps} reps"
    lines.append(f"[bold]{ex.name}[/bold]")
    if ex.weight:
        lines.append(f"Weight: {ex.weight}")
    lines.append(f"Set {set_num} of {ex.total_sets}  •  {reps_label}")
    if status:
        lines.append(f"\n{status}")
    if timer_text:
        lines.append(f"\n[{timer_style}]{timer_text}[/{timer_style}]")
    return Panel("\n".join(lines), title="Current", border_style="cyan", expand=True)


def build_progress_bar(exercises: list[Exercise]) -> str:
    total = sum(e.total_sets for e in exercises)
    done = sum(e.completed_sets for e in exercises)
    pct = done / total * 100 if total else 100
    bar_len = 30
    filled = int(bar_len * done / total) if total else bar_len
    bar = "█" * filled + "░" * (bar_len - filled)
    return f"[bold cyan]Progress:[/bold cyan] {bar} {done}/{total} sets ({pct:.0f}%)"


# ---------------------------------------------------------------------------
# Rest timer
# ---------------------------------------------------------------------------

def rest_timer(
    exercises: list[Exercise],
    current_idx: int,
    rest_seconds: int,
    live: Live,
    ex: Exercise,
    set_num: int,
) -> None:
    """Countdown rest timer. Enter to skip. Voice nags when overtime."""
    say(random.choice(REST_MESSAGES))
    start = time.time()
    nag_interval = 15
    nagged_at = 0

    enter_cbreak()
    drain_stdin()

    try:
        while True:
            elapsed = time.time() - start
            remaining = rest_seconds - elapsed
            overtime = remaining < 0

            if overtime:
                overtime_secs = int(-remaining)
                timer_text = f"OVERTIME +{overtime_secs}s  (press Enter to continue)"
                timer_style = "bold red"
                if overtime_secs >= nag_interval and overtime_secs // nag_interval > nagged_at:
                    nagged_at = overtime_secs // nag_interval
                    say("Time's up, let's go")
            else:
                secs_left = int(remaining) + 1
                timer_text = f"Rest: {secs_left}s remaining  (press Enter to skip)"
                timer_style = "bold yellow"
                if remaining <= 0.5 and nagged_at == 0:
                    nagged_at = -1
                    say("Time's up, let's go")

            overview = build_overview(exercises, current_idx)
            panel = build_active_panel(
                ex, set_num, status="Resting...", timer_text=timer_text, timer_style=timer_style
            )
            progress_text = build_progress_bar(exercises)

            layout = Table.grid(expand=True)
            layout.add_row(overview)
            layout.add_row(panel)
            layout.add_row(Text.from_markup(progress_text))

            live.update(layout)

            if stdin_ready():
                os.read(sys.stdin.fileno(), 1024)
                break

            time.sleep(0.25)
    finally:
        restore_terminal()


# ---------------------------------------------------------------------------
# Timed hold
# ---------------------------------------------------------------------------

def timed_hold(
    exercises: list[Exercise],
    current_idx: int,
    live: Live,
    ex: Exercise,
    set_num: int,
) -> None:
    """Run a timed hold with voice cues."""
    duration = ex.reps

    # Get in position
    say_sync("Get in position")
    for countdown in range(3, 0, -1):
        overview = build_overview(exercises, current_idx)
        panel = build_active_panel(
            ex, set_num, status="Get in position...", timer_text=str(countdown), timer_style="bold yellow"
        )
        progress_text = build_progress_bar(exercises)
        layout = Table.grid(expand=True)
        layout.add_row(overview)
        layout.add_row(panel)
        layout.add_row(Text.from_markup(progress_text))
        live.update(layout)
        time.sleep(1)

    say("Go")

    # Pick two distinct motivational messages for mid and 75% marks
    hold_msgs = random.sample(HOLD_MESSAGES, min(2, len(HOLD_MESSAGES)))
    mid_said = False
    three_quarter_said = False

    start = time.time()
    while True:
        elapsed = time.time() - start
        remaining = duration - elapsed
        if remaining <= 0:
            break

        pct = elapsed / duration
        if pct >= 0.5 and not mid_said:
            mid_said = True
            say(hold_msgs[0])
        elif pct >= 0.75 and not three_quarter_said:
            three_quarter_said = True
            say(hold_msgs[1] if len(hold_msgs) > 1 else hold_msgs[0])

        secs_left = int(remaining) + 1
        overview = build_overview(exercises, current_idx)
        panel = build_active_panel(
            ex, set_num, status="HOLD!", timer_text=f"{secs_left}s", timer_style="bold green"
        )
        progress_text = build_progress_bar(exercises)
        layout = Table.grid(expand=True)
        layout.add_row(overview)
        layout.add_row(panel)
        layout.add_row(Text.from_markup(progress_text))
        live.update(layout)
        time.sleep(0.25)

    say("Done")


# ---------------------------------------------------------------------------
# Main workout loop
# ---------------------------------------------------------------------------

def run_workout(exercises: list[Exercise], rest_seconds: int) -> None:
    console = Console()

    # Find first incomplete exercise
    start_idx = 0
    for i, ex in enumerate(exercises):
        if not ex.done:
            start_idx = i
            break
    else:
        console.print("[green]All exercises already complete![/green]")
        print_log(exercises)
        return

    with Live(console=console, refresh_per_second=4, screen=True) as live:
        for ex_idx in range(start_idx, len(exercises)):
            ex = exercises[ex_idx]
            if ex.done:
                continue

            # Announce exercise
            weight_say = f", {ex.weight}" if ex.weight and ex.weight != "BW" else ""
            say(f"Next exercise: {ex.name}{weight_say}")

            start_set = ex.completed_sets + 1
            for set_num in range(start_set, ex.total_sets + 1):
                say(f"Set {set_num} of {ex.total_sets}")

                if ex.timed:
                    # Timed hold
                    timed_hold(exercises, ex_idx, live, ex, set_num)
                else:
                    # Rep-based: show panel, wait for Enter
                    enter_cbreak()
                    drain_stdin()
                    try:
                        while True:
                            overview = build_overview(exercises, ex_idx)
                            reps_label = f"{ex.reps} reps"
                            panel = build_active_panel(
                                ex, set_num,
                                status=f"Do {reps_label}, then press Enter",
                            )
                            progress_text = build_progress_bar(exercises)
                            layout = Table.grid(expand=True)
                            layout.add_row(overview)
                            layout.add_row(panel)
                            layout.add_row(Text.from_markup(progress_text))
                            live.update(layout)
                            if stdin_ready():
                                os.read(sys.stdin.fileno(), 1024)
                                break
                            time.sleep(0.25)
                    finally:
                        restore_terminal()

                # Mark set complete
                ex.completed_sets = set_num
                play_sound(_SOUND_SET_COMPLETE)
                save_state(exercises)

                # Rest timer unless last set of last exercise
                is_last_set_of_exercise = set_num == ex.total_sets
                is_last_exercise = ex_idx == len(exercises) - 1
                if not (is_last_set_of_exercise and is_last_exercise):
                    if is_last_set_of_exercise:
                        play_sound(_SOUND_EXERCISE_COMPLETE)
                        say(f"{ex.name} complete!")
                    rest_timer(exercises, ex_idx, rest_seconds, live, ex, set_num)

    say("Workout complete! Great job.")
    console.print("[bold green]Workout complete![/bold green]\n")
    print_log(exercises)
    clear_state()


def print_log(exercises: list[Exercise]) -> None:
    print("\n" + format_log(exercises) + "\n")


# ---------------------------------------------------------------------------
# Input handling
# ---------------------------------------------------------------------------

def read_input(file_path: str | None) -> str:
    if file_path:
        return Path(file_path).read_text()

    console = Console(stderr=True)
    console.print(
        "[bold cyan]Paste your workout below, then press Enter on an empty line:[/bold cyan]"
    )
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == "" and lines:
            break
        lines.append(line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Resume logic
# ---------------------------------------------------------------------------

def try_resume(parsed: list[Exercise]) -> list[Exercise]:
    console = Console(stderr=True)
    result = load_state()
    if result is None:
        return parsed

    saved, ts = result
    age_hours = (time.time() - ts) / 3600

    if not exercises_match(parsed, saved):
        console.print("[yellow]Saved state doesn't match current workout, starting fresh.[/yellow]")
        clear_state()
        return parsed

    total_done = sum(e.completed_sets for e in saved)
    if total_done == 0:
        clear_state()
        return parsed

    if age_hours > STALE_HOURS:
        console.print(
            f"[yellow]Found saved state from {age_hours:.1f} hours ago.[/yellow]"
        )

    done_total = sum(e.total_sets for e in saved)
    console.print(
        f"[cyan]Saved progress found: {total_done}/{done_total} sets complete.[/cyan]"
    )
    console.print("[cyan]Resume? (Y/n):[/cyan] ", end="")

    try:
        answer = input().strip().lower()
    except EOFError:
        answer = "y"

    if answer in ("", "y", "yes"):
        console.print("[green]Resuming...[/green]")
        return saved
    else:
        clear_state()
        return parsed


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Workout Coach TUI")
    parser.add_argument("file", nargs="?", help="Workout file to read")
    parser.add_argument("--rest", type=int, default=75, help="Rest seconds between sets (default: 75)")
    parser.add_argument("--reset", action="store_true", help="Discard saved state and exit")
    parser.add_argument("--log", action="store_true", help="Print current saved log and exit")
    args = parser.parse_args()

    if args.reset:
        clear_state()
        print("State cleared.")
        return

    if args.log:
        result = load_state()
        if result:
            exercises, _ = result
            print(format_log(exercises))
        else:
            print("No saved state.")
        return

    text = read_input(args.file)
    exercises = parse_workout(text)

    if not exercises:
        print("No exercises parsed. Check your input format.", file=sys.stderr)
        sys.exit(1)

    exercises = try_resume(exercises)

    try:
        run_workout(exercises, args.rest)
    except KeyboardInterrupt:
        restore_terminal()
        save_state(exercises)
        # Kill any lingering say process
        if _say_proc and _say_proc.poll() is None:
            _say_proc.terminate()
        print("\n\nWorkout paused. Progress saved.\n")
        print_log(exercises)


if __name__ == "__main__":
    main()
