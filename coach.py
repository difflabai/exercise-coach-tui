#!/usr/bin/env python3
"""Workout Coach TUI — cassette-player architecture for structured workout sessions."""

import argparse
import hashlib
import json
import os
import re
import select
import signal
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
from rich.text import Text
from rich import box

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STATE_FILE = Path(__file__).parent / ".workout_state.json"


class WorkoutPaused(Exception):
    """Raised when user presses Ctrl-Z to suspend to shell."""
    pass
STALE_HOURS = 4

OVERTIME_NAGS = [
    "Rest is over, let's go.",
    "Time's up.",
    "Clock's done, you're not.",
    "Let's move.",
]

# ---------------------------------------------------------------------------
# Cassette data model
# ---------------------------------------------------------------------------


@dataclass
class SetData:
    reps: int  # target reps or seconds
    actual_reps: int | None = None
    failure: bool = False


@dataclass
class ExerciseData:
    name: str
    load: str
    timed: bool
    sets: list[SetData] = field(default_factory=list)


@dataclass
class TimedCue:
    at_seconds: int
    line: str


@dataclass
class Group:
    type: str  # "straight", "superset", "circuit"
    rounds: int
    rest: int  # resolved (group.rest or meta.rest_default)
    exercises: list[ExerciseData] = field(default_factory=list)
    voice_intro: str | None = None
    voice_round_complete: list[str] = field(default_factory=list)
    voice_group_complete: str | None = None
    voice_during_set: list[list[TimedCue]] = field(default_factory=list)
    setup: str | None = None
    skipped: bool = False


@dataclass
class Phase:
    type: str  # "warmup", "main", "cooldown"
    voice_intro: str | None = None
    groups: list[Group] = field(default_factory=list)


@dataclass
class ContextExercise:
    name: str
    note: str
    voice: str | None = None


@dataclass
class Cassette:
    version: str
    meta: dict
    phases: list[Phase] = field(default_factory=list)
    context_exercises: list[ContextExercise] = field(default_factory=list)
    voice_session_intro: str | None = None
    voice_session_complete: str | None = None


# ---------------------------------------------------------------------------
# Legacy data model (for text input backward compat)
# ---------------------------------------------------------------------------

SET_RE = re.compile(
    r"(\d+)"           # total sets
    r"(?:\[(\d+)\])?"  # optional [completed]
    r"[×x]"            # separator
    r"(\d+)(s)?"       # reps or seconds
)


@dataclass
class Exercise:
    name: str
    total_sets: int
    reps: int
    timed: bool
    weight: str
    phase: str
    completed_sets: int = 0

    @property
    def done(self) -> bool:
        return self.completed_sets >= self.total_sets

    def log_str(self, show_progress: bool = True) -> str:
        reps_str = f"{self.reps}s" if self.timed else str(self.reps)
        weight_part = f" | {self.weight}" if self.weight else ""
        if show_progress:
            return f"{self.name:<25} {self.total_sets}[{self.completed_sets}]×{reps_str}{weight_part}"
        return f"{self.name:<25} {self.total_sets}×{reps_str}{weight_part}"


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
        name=name, total_sets=total_sets, reps=reps, timed=timed,
        weight=weight, phase=phase, completed_sets=completed,
    )


def parse_workout(text: str) -> list[Exercise]:
    exercises = []
    for line in text.splitlines():
        ex = parse_exercise(line)
        if ex:
            exercises.append(ex)
    return exercises


# ---------------------------------------------------------------------------
# Cassette loading
# ---------------------------------------------------------------------------

def load_cassette(path: str) -> Cassette:
    """Load and validate a cassette JSON file."""
    return load_cassette_from_dict(json.loads(Path(path).read_text()))


def load_cassette_from_dict(data: dict) -> Cassette:
    """Build a Cassette from a parsed JSON dict."""
    rest_default = data.get("meta", {}).get("rest_default", 75)

    phases = []
    for phase_data in data.get("phases", []):
        groups = []
        for g in phase_data.get("groups", []):
            rest = g.get("rest") or rest_default

            exercises = []
            for ex in g["exercises"]:
                sets = [SetData(reps=s["reps"]) for s in ex.get("sets", [])]
                # If sets not specified, generate from rounds
                if not sets:
                    default_reps = ex.get("reps", 0)
                    sets = [SetData(reps=default_reps) for _ in range(g.get("rounds", 1))]
                exercises.append(ExerciseData(
                    name=ex["name"], load=ex.get("load", ""),
                    timed=ex.get("timed", False), sets=sets,
                ))

            timed_cues = []
            for round_cues in g.get("voice_during_set", []):
                timed_cues.append([
                    TimedCue(at_seconds=c["at_seconds"], line=c["line"])
                    for c in round_cues
                ])

            groups.append(Group(
                type=g.get("type", "straight"),
                rounds=g.get("rounds", 1),
                rest=rest,
                exercises=exercises,
                voice_intro=g.get("voice_intro"),
                voice_round_complete=g.get("voice_round_complete", []),
                voice_group_complete=g.get("voice_group_complete"),
                voice_during_set=timed_cues,
                setup=g.get("setup"),
            ))
        phases.append(Phase(
            type=phase_data.get("type", "main"),
            voice_intro=phase_data.get("voice_intro"),
            groups=groups,
        ))

    ctx = [
        ContextExercise(name=c["name"], note=c["note"], voice=c.get("voice"))
        for c in data.get("context_exercises", [])
    ]

    return Cassette(
        version=data.get("version", "1.0"),
        meta=data.get("meta", {}),
        phases=phases,
        context_exercises=ctx,
        voice_session_intro=data.get("voice", {}).get("session_intro"),
        voice_session_complete=data.get("voice", {}).get("session_complete"),
    )


def text_to_cassette(exercises: list[Exercise], rest: int) -> Cassette:
    """Wrap legacy parsed exercises in a cassette with no voice lines."""
    groups = []
    for ex in exercises:
        groups.append(Group(
            type="straight",
            rounds=ex.total_sets,
            rest=rest,
            exercises=[ExerciseData(
                name=ex.name,
                load=ex.weight,
                timed=ex.timed,
                sets=[SetData(reps=ex.reps) for _ in range(ex.total_sets)],
            )],
        ))
    return Cassette(
        version="1.1",
        meta={"date": "", "title": "Workout", "rest_default": rest},
        phases=[Phase(type="main", groups=groups)],
    )


def cassette_content_hash(path: str) -> str:
    """SHA256 of the cassette file contents for state verification."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Sound effects
# ---------------------------------------------------------------------------

def _generate_tone(frequency: int, duration_ms: int, volume: float = 0.5) -> bytes:
    """Generate a WAV tone in memory. Returns raw WAV bytes."""
    import struct
    import wave
    import io
    import math
    sample_rate = 44100
    n_samples = int(sample_rate * duration_ms / 1000)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        frames = b"".join(
            struct.pack("<h", int(volume * 32767 * math.sin(2 * math.pi * frequency * i / sample_rate)))
            for i in range(n_samples)
        )
        wf.writeframes(frames)
    return buf.getvalue()


def _generate_reward_tone() -> bytes:
    """A short rising two-note chime for set completion."""
    import io, wave
    tone1 = _generate_tone(880, 120, 0.4)
    tone2 = _generate_tone(1175, 200, 0.4)
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
    """A three-note ascending chime for exercise/group completion."""
    import io, wave
    tones = [
        _generate_tone(784, 100, 0.4),
        _generate_tone(988, 100, 0.4),
        _generate_tone(1319, 250, 0.4),
    ]
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(44100)
        for t in tones:
            with wave.open(io.BytesIO(t), "rb") as w:
                out.writeframes(w.readframes(w.getnframes()))
    return buf.getvalue()


_SOUND_SET_COMPLETE = _generate_reward_tone()
_SOUND_GROUP_COMPLETE = _generate_exercise_complete_tone()

# ---------------------------------------------------------------------------
# Voice
# ---------------------------------------------------------------------------

_say_proc: subprocess.Popen | None = None


def play_sound(sound_data: bytes) -> None:
    """Play a WAV sound from bytes (non-blocking)."""
    import tempfile
    try:
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.write(sound_data)
        tmp.close()
        for cmd in (["afplay", tmp.name], ["aplay", "-q", tmp.name]):
            try:
                subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except FileNotFoundError:
                continue
    except OSError:
        pass


def say(text: str) -> None:
    """Non-blocking speech."""
    global _say_proc
    try:
        if _say_proc and _say_proc.poll() is None:
            _say_proc.terminate()
        _say_proc = subprocess.Popen(
            ["say", text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        pass


def say_sync(text: str, wait: float = 0) -> None:
    """Blocking speech."""
    global _say_proc
    try:
        if _say_proc and _say_proc.poll() is None:
            _say_proc.terminate()
        _say_proc = subprocess.Popen(
            ["say", text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        _say_proc.wait()
        if wait > 0:
            time.sleep(wait)
    except FileNotFoundError:
        if wait > 0:
            time.sleep(wait)


def speak(line: str | None) -> None:
    """Say a line if it exists. Skip silently if null/empty."""
    if line:
        say(line)


def speak_sync(line: str | None, wait: float = 0) -> None:
    """Blocking speak with null safety."""
    if line:
        say_sync(line, wait)


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


# ---------------------------------------------------------------------------
# Cassette helpers
# ---------------------------------------------------------------------------

def all_groups(cassette: Cassette) -> list[tuple[int, int, Group]]:
    """Yield (phase_idx, group_idx, group) for every group in order."""
    result = []
    for pi, phase in enumerate(cassette.phases):
        for gi, group in enumerate(phase.groups):
            result.append((pi, gi, group))
    return result


def count_sets(cassette: Cassette) -> tuple[int, int]:
    """Return (total_sets, completed_sets) across the whole cassette."""
    total = 0
    done = 0
    for phase in cassette.phases:
        for group in phase.groups:
            for ex in group.exercises:
                total += len(ex.sets)
                done += sum(1 for s in ex.sets if s.actual_reps is not None)
    return total, done


def undo_last_set(group: Group) -> bool:
    """Reset the most recently completed set in a group. Returns True if a set was undone."""
    last_round = -1
    last_ei = -1
    for r in range(group.rounds):
        for ei, ex in enumerate(group.exercises):
            if r < len(ex.sets) and ex.sets[r].actual_reps is not None:
                last_round = r
                last_ei = ei
    if last_round < 0:
        return False
    s = group.exercises[last_ei].sets[last_round]
    s.actual_reps = None
    s.failure = False
    return True


def undo_last_set_global(cassette: Cassette) -> tuple[int, int] | None:
    """Undo the most recently completed set across the entire cassette.
    Returns (phase_idx, group_idx) of the undone set, or None."""
    last_pi, last_gi, last_round, last_ei = -1, -1, -1, -1
    for pi, phase in enumerate(cassette.phases):
        for gi, group in enumerate(phase.groups):
            if group.skipped:
                continue
            for r in range(group.rounds):
                for ei, ex in enumerate(group.exercises):
                    if r < len(ex.sets) and ex.sets[r].actual_reps is not None:
                        last_pi, last_gi, last_round, last_ei = pi, gi, r, ei
    if last_pi < 0:
        return None
    grp = cassette.phases[last_pi].groups[last_gi]
    s = grp.exercises[last_ei].sets[last_round]
    s.actual_reps = None
    s.failure = False
    grp.skipped = False
    return (last_pi, last_gi)


def rounds_completed(group: Group) -> int:
    """Count how many full rounds are completed in a group."""
    if not group.exercises:
        return 0
    # A round is complete when all exercises have actual_reps for that round index
    for r in range(group.rounds):
        for ex in group.exercises:
            if r >= len(ex.sets) or ex.sets[r].actual_reps is None:
                return r
    return group.rounds



def get_cues_for_round(group: Group, round_idx: int) -> list[TimedCue]:
    if round_idx < len(group.voice_during_set):
        return group.voice_during_set[round_idx]
    return []


def speak_round_complete(group: Group, round_idx: int) -> None:
    if round_idx < len(group.voice_round_complete):
        speak(group.voice_round_complete[round_idx])


# ---------------------------------------------------------------------------
# TUI rendering
# ---------------------------------------------------------------------------

def build_overview(cassette: Cassette, cur_phase: int, cur_group: int) -> Table:
    """Build the overview table showing all phases, groups, and exercises."""
    title = cassette.meta.get("title", "Workout")
    program = cassette.meta.get("program", "")
    if program:
        title = f"{title} — {program}"

    table = Table(
        title=title, box=box.SIMPLE_HEAVY, show_header=False,
        pad_edge=False, expand=True,
    )
    table.add_column("Exercise", ratio=1)

    for pi, phase in enumerate(cassette.phases):
        # Phase header
        table.add_row(Text(f" {phase.type.upper()}", style="bold underline"))

        for gi, group in enumerate(phase.groups):
            is_current = pi == cur_phase and gi == cur_group
            n_ex = len(group.exercises)
            rc = rounds_completed(group)

            for ei, ex in enumerate(group.exercises):
                reps_str = f"{ex.sets[0].reps}s" if ex.timed else str(ex.sets[0].reps)
                load_str = f" | {ex.load}" if ex.load else ""

                if group.skipped:
                    label = f"{ex.name:<25} {group.rounds}[0]×{reps_str}{load_str}"
                    style = "dim yellow"
                elif rc >= group.rounds:
                    label = f"{ex.name:<25} {group.rounds}[{rc}]×{reps_str}{load_str}"
                    style = "dim green"
                elif is_current:
                    label = f"{ex.name:<25} {group.rounds}[{rc}]×{reps_str}{load_str}"
                    style = "bold white on blue"
                else:
                    label = f"{ex.name:<25} {group.rounds}×{reps_str}{load_str}"
                    style = ""

                # Superset/circuit connectors
                prefix = "  "
                if n_ex > 1:
                    if ei == 0:
                        prefix = "  ┌ "
                    elif ei == n_ex - 1:
                        prefix = "  └ "
                    else:
                        prefix = "  ├ "
                else:
                    prefix = "    "

                text = Text(f"{prefix}{label}")
                if style:
                    text.stylize(style)
                table.add_row(text)

        table.add_row(Text(""))  # spacing between phases

    # Context exercises
    if cassette.context_exercises:
        table.add_row(Text(" ── Context ──", style="dim"))
        for ctx in cassette.context_exercises:
            table.add_row(Text(f"    {ctx.name}: {ctx.note}", style="dim"))

    return table


def build_active_panel_straight(
    ex: ExerciseData, round_idx: int, total_rounds: int,
    status: str = "", timer_text: str = "", timer_style: str = "bold white",
) -> Panel:
    """Active panel for straight sets (single exercise group)."""
    lines: list[str] = []
    reps_label = f"{ex.sets[round_idx].reps}s hold" if ex.timed else f"{ex.sets[round_idx].reps} reps"
    lines.append(f"[bold]{ex.name}[/bold]")
    if ex.load:
        lines.append(f"Weight: {ex.load}")
    lines.append(f"Round {round_idx + 1} of {total_rounds}  •  {reps_label}")
    if status:
        lines.append(f"\n{status}")
    if timer_text:
        lines.append(f"\n[{timer_style}]{timer_text}[/{timer_style}]")
    return Panel("\n".join(lines), title="Current", border_style="cyan", expand=True)


def build_active_panel_superset(
    group: Group, round_idx: int, active_ex_idx: int,
    status: str = "", timer_text: str = "", timer_style: str = "bold white",
) -> Panel:
    """Active panel for supersets/circuits showing all exercises with markers."""
    lines: list[str] = []
    for ei, ex in enumerate(group.exercises):
        reps_label = f"{ex.sets[round_idx].reps}s" if ex.timed else f"{ex.sets[round_idx].reps} reps"
        load_str = f"  •  {ex.load}" if ex.load else ""
        if ei < active_ex_idx:
            marker = "✓"
        elif ei == active_ex_idx:
            marker = "►"
        else:
            marker = " "
        lines.append(f"{marker} {ex.name}  •  {reps_label}{load_str}")
    if status:
        lines.append(f"\n{status}")
    if timer_text:
        lines.append(f"\n[{timer_style}]{timer_text}[/{timer_style}]")
    title = f"Superset (Round {round_idx + 1} of {group.rounds})" if group.type == "superset" else f"Circuit (Round {round_idx + 1} of {group.rounds})"
    return Panel("\n".join(lines), title=title, border_style="cyan", expand=True)


def build_active_panel(
    cassette: Cassette, group: Group, ex: ExerciseData,
    round_idx: int, ex_idx: int,
    status: str = "", timer_text: str = "", timer_style: str = "bold white",
) -> Panel:
    """Route to the right panel type based on group structure."""
    if len(group.exercises) == 1:
        return build_active_panel_straight(ex, round_idx, group.rounds, status, timer_text, timer_style)
    return build_active_panel_superset(group, round_idx, ex_idx, status, timer_text, timer_style)


def build_rest_panel(rest_seconds: int, remaining: float, overtime: bool) -> Panel:
    """Panel shown during rest periods."""
    if overtime:
        ot_secs = int(-remaining)
        timer_text = f"OVERTIME +{ot_secs}s  (Enter to continue • b = back • p = pause)"
        timer_style = "bold red"
    else:
        secs_left = int(remaining) + 1
        timer_text = f"{secs_left}s remaining  (Enter to skip • b = back • p = pause)"
        timer_style = "bold yellow"
    content = f"Resting...\n\n[{timer_style}]{timer_text}[/{timer_style}]"
    return Panel(content, title="Rest", border_style="yellow", expand=True)


def format_eta(seconds: int) -> str:
    if seconds <= 0:
        return "done"
    minutes, secs = divmod(seconds, 60)
    if minutes > 0:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def estimate_remaining(cassette: Cassette, avg_rep_set: float = 30.0) -> int:
    """Estimate seconds remaining based on cassette state."""
    remaining = 0
    remaining_sets = 0
    rest_total = 0
    for phase in cassette.phases:
        for group in phase.groups:
            if group.skipped:
                continue
            for ex in group.exercises:
                for s in ex.sets:
                    if s.actual_reps is None:
                        if ex.timed:
                            remaining += s.reps + 3  # hold + countdown
                        else:
                            remaining += int(avg_rep_set)
                        remaining_sets += 1
            # Estimate rest for remaining rounds
            rc = rounds_completed(group)
            rounds_left = group.rounds - rc
            if rounds_left > 0:
                rest_total += (rounds_left - 1) * group.rest
    remaining += rest_total
    return remaining


def build_progress_bar(cassette: Cassette, avg_rep_set: float = 30.0) -> str:
    total, done = count_sets(cassette)
    pct = done / total * 100 if total else 100
    bar_len = 30
    filled = int(bar_len * done / total) if total else bar_len
    bar = "█" * filled + "░" * (bar_len - filled)
    eta = format_eta(estimate_remaining(cassette, avg_rep_set))
    return f"[bold cyan]Progress:[/bold cyan] {bar} {done}/{total} sets ({pct:.0f}%)  ⏱ ETA: {eta}"


def render_layout(live: Live, overview: Table, panel: Panel, progress_text: str) -> None:
    """Compose and push a full screen update."""
    layout = Table.grid(expand=True)
    layout.add_row(overview)
    layout.add_row(panel)
    layout.add_row(Text.from_markup(progress_text))
    live.update(layout)


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def save_state(cassette: Cassette, position: dict, cassette_path: str | None = None) -> None:
    """Save current playback state."""
    groups_state = []
    for pi, phase in enumerate(cassette.phases):
        for gi, group in enumerate(phase.groups):
            sets_data = []
            for ex in group.exercises:
                ex_sets = []
                for s in ex.sets:
                    ex_sets.append({
                        "actual_reps": s.actual_reps,
                        "failure": s.failure,
                    })
                sets_data.append(ex_sets)
            groups_state.append({
                "phase_idx": pi,
                "group_idx": gi,
                "skipped": group.skipped,
                "rounds_completed": rounds_completed(group),
                "sets": sets_data,
            })

    data = {
        "timestamp": time.time(),
        "cassette_hash": cassette_content_hash(cassette_path) if cassette_path else "",
        "position": position,
        "groups_state": groups_state,
    }
    STATE_FILE.write_text(json.dumps(data, indent=2))


def load_state_data() -> dict | None:
    """Load raw state dict from file."""
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def apply_state(cassette: Cassette, state: dict) -> dict:
    """Apply saved state to a cassette. Returns the resume position."""
    groups_state = state.get("groups_state", [])
    flat_groups = all_groups(cassette)

    for gs in groups_state:
        pi, gi = gs["phase_idx"], gs["group_idx"]
        # Find matching group
        for fpi, fgi, group in flat_groups:
            if fpi == pi and fgi == gi:
                group.skipped = gs.get("skipped", False)
                sets_data = gs.get("sets", [])
                for ei, ex in enumerate(group.exercises):
                    if ei < len(sets_data):
                        for ri, s_data in enumerate(sets_data[ei]):
                            if ri < len(ex.sets):
                                ex.sets[ri].actual_reps = s_data.get("actual_reps")
                                ex.sets[ri].failure = s_data.get("failure", False)
                break

    return state.get("position", {"phase_idx": 0, "group_idx": 0, "round_idx": 0})


def clear_state() -> None:
    if STATE_FILE.exists():
        STATE_FILE.unlink()


# ---------------------------------------------------------------------------
# Log output
# ---------------------------------------------------------------------------

def format_exercise_log(ex: ExerciseData, group: Group) -> str:
    reps_str = f"{ex.sets[0].reps}s" if ex.timed else str(ex.sets[0].reps)
    load_str = f" | {ex.load}" if ex.load else ""
    program = ""  # program info lives in meta, not per-exercise

    if group.skipped:
        return f"{ex.name:<25} {group.rounds}[0]×{reps_str}{load_str}"

    failures = [(i, s) for i, s in enumerate(ex.sets) if s.failure]
    completed_count = sum(1 for s in ex.sets if s.actual_reps is not None)

    if failures:
        fail_idx, fail_set = failures[0]
        fail_reps = fail_set.actual_reps or 0
        return (
            f"{ex.name:<25} {group.rounds}[{fail_idx}]×{reps_str}"
            f" - failed at {fail_reps} on set {fail_idx + 1}{load_str}"
        )

    if completed_count >= group.rounds:
        return f"{ex.name:<25} {group.rounds}×{reps_str}{load_str}"

    return f"{ex.name:<25} {group.rounds}[{completed_count}]×{reps_str}{load_str}"


def render_log(cassette: Cassette) -> str:
    lines = []
    for ctx in cassette.context_exercises:
        lines.append(f"{ctx.name:<25} — (see notes)")
    for phase in cassette.phases:
        for group in phase.groups:
            for ex in group.exercises:
                lines.append(format_exercise_log(ex, group))
    return "\n".join(lines)


def print_log(cassette: Cassette) -> None:
    print("\n" + render_log(cassette) + "\n")


# ---------------------------------------------------------------------------
# Pause
# ---------------------------------------------------------------------------

def pause_screen(
    live: Live, cassette: Cassette, cur_phase: int, cur_group: int,
    avg_rep_set: float = 30.0,
) -> float:
    """Show pause overlay. Returns seconds spent paused."""
    global _say_proc
    if _say_proc and _say_proc.poll() is None:
        _say_proc.terminate()

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
) -> None:
    """Show a setup/transition screen between groups. Blocks until Enter."""
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
    content += "\n\n[dim]Press Enter when ready[/dim]"

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
            elif key == "ctrl-z":
                raise WorkoutPaused()

            time.sleep(0.25)
    finally:
        restore_terminal()


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

    enter_cbreak()
    drain_stdin()

    try:
        while True:
            elapsed = time.time() - start
            remaining = rest_seconds - elapsed
            overtime = remaining < 0

            if overtime:
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
                    transition_screen(live, cassette, pi, gi, group, avg_rep_set())
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

                    go_back = False

                    for ei, ex in enumerate(group.exercises):
                        if skip_group or go_back or jump_back is not None:
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
                                undo_result = undo_last_set_global(cassette)
                                if undo_result is not None:
                                    if undo_result == (pi, gi):
                                        go_back = True
                                    else:
                                        jump_back = undo_result
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
                                        undo_result = undo_last_set_global(cassette)
                                        if undo_result is not None:
                                            if undo_result == (pi, gi):
                                                go_back = True
                                            else:
                                                jump_back = undo_result
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

                            if not skip_group and not go_back and jump_back is None:
                                rep_set_durations.append(time.time() - set_start)

                        if skip_group or go_back or jump_back is not None:
                            break

                        # Set complete
                        play_sound(_SOUND_SET_COMPLETE)

                    if jump_back is not None:
                        break

                    if go_back:
                        # Recalculate position after undo
                        round_idx = rounds_completed(group)
                        continue

                    if skip_group:
                        group.skipped = True
                        say(f"Skipping {group.exercises[0].name}")
                        save_state(cassette, {"phase_idx": pi, "group_idx": gi, "round_idx": round_idx}, cassette_path)
                        break

                    # Round complete
                    speak_round_complete(group, round_idx)
                    play_sound(_SOUND_SET_COMPLETE)
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
                            undo_result = undo_last_set_global(cassette)
                            if undo_result is not None:
                                if undo_result == (pi, gi):
                                    round_idx = rounds_completed(group)
                                    continue
                                else:
                                    jump_back = undo_result
                                    break
                            round_idx = rounds_completed(group)
                            continue

                    round_idx += 1

                if jump_back is not None:
                    break

                # Group complete
                if not group.skipped:
                    speak(group.voice_group_complete)
                    play_sound(_SOUND_GROUP_COMPLETE)

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
    clear_state()


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


def parse_input(text: str, rest: int) -> tuple[Cassette, bool]:
    """Parse input text. Returns (cassette, is_json). Tries JSON first, then text format."""
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
            if "phases" in data:
                return load_cassette_from_dict(data), True
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
    exercises = parse_workout(text)
    return text_to_cassette(exercises, rest), False


# ---------------------------------------------------------------------------
# Resume logic
# ---------------------------------------------------------------------------

def try_resume(cassette: Cassette, cassette_path: str | None, auto: bool = False) -> dict | None:
    """Check for saved state and optionally resume. Returns resume position or None."""
    console = Console(stderr=True)
    state = load_state_data()
    if state is None:
        return None

    ts = state.get("timestamp", 0)
    age_hours = (time.time() - ts) / 3600

    # Verify cassette hash if we have a file path
    if cassette_path:
        saved_hash = state.get("cassette_hash", "")
        current_hash = cassette_content_hash(cassette_path)
        if saved_hash and saved_hash != current_hash:
            if not auto:
                console.print("[yellow]Saved state doesn't match this cassette, starting fresh.[/yellow]")
            clear_state()
            return None

    # Check if there's any progress
    groups_state = state.get("groups_state", [])
    total_done = sum(
        sum(1 for ex_sets in gs.get("sets", []) for s in ex_sets if s.get("actual_reps") is not None)
        for gs in groups_state
    )
    if total_done == 0:
        clear_state()
        return None

    total_sets, _ = count_sets(cassette)

    if age_hours > STALE_HOURS:
        console.print(f"[yellow]Found saved state from {age_hours:.1f} hours ago.[/yellow]")

    console.print(f"[cyan]Saved progress found: {total_done}/{total_sets} sets complete.[/cyan]")

    if auto:
        console.print("[green]Resuming...[/green]")
        return apply_state(cassette, state)

    console.print("[cyan]Resume? (Y/n):[/cyan] ", end="")
    try:
        answer = input().strip().lower()
    except EOFError:
        answer = "y"

    if answer in ("", "y", "yes"):
        console.print("[green]Resuming...[/green]")
        return apply_state(cassette, state)
    else:
        clear_state()
        return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Workout Coach TUI")
    parser.add_argument("file", nargs="?", help="Workout file (.json cassette or .txt)")
    parser.add_argument("--rest", type=int, default=75, help="Rest seconds (default: 75, overrides cassette default)")
    parser.add_argument("--resume", action="store_true", help="Resume last workout without prompting")
    parser.add_argument("--reset", "--restart", action="store_true", help="Discard saved state and exit")
    parser.add_argument("--log", action="store_true", help="Print current saved log and exit")
    args = parser.parse_args()

    if args.reset:
        clear_state()
        print("State cleared.")
        return

    if args.log:
        state = load_state_data()
        if not state:
            print("No saved state.")
            return
        if not args.file:
            print("Provide the cassette file: python coach.py workout.json --log", file=sys.stderr)
            sys.exit(1)
        if args.file.endswith(".json"):
            cassette = load_cassette(args.file)
        else:
            text = Path(args.file).read_text()
            cassette, _ = parse_input(text, args.rest)
        apply_state(cassette, state)
        print(render_log(cassette))
        return

    cassette_path = None

    if args.resume:
        # Pure resume: load state, then we need the cassette file
        state = load_state_data()
        if state is None:
            print("No saved state to resume.", file=sys.stderr)
            sys.exit(1)
        # Can't fully resume without a cassette file — need one
        if not args.file:
            print("Provide the cassette file to resume: python coach.py workout.json --resume", file=sys.stderr)
            sys.exit(1)
        cassette_path = args.file
        if args.file.endswith(".json"):
            cassette = load_cassette(args.file)
        else:
            text = Path(args.file).read_text()
            cassette, _ = parse_input(text, args.rest)
        position = apply_state(cassette, state)
    else:
        text = read_input(args.file)
        cassette, is_json = parse_input(text, args.rest)

        if not cassette.phases or not any(g for p in cassette.phases for g in p.groups):
            print("No exercises parsed. Check your input format.", file=sys.stderr)
            sys.exit(1)

        if args.file and args.file.endswith(".json"):
            cassette_path = args.file

        # Apply --rest override: always for text input, only when explicitly set for JSON
        rest_override = not is_json or args.rest != 75
        if rest_override:
            for phase in cassette.phases:
                for group in phase.groups:
                    group.rest = args.rest

        # Try resume
        try_resume(cassette, cassette_path)

    def _save_current_position():
        pos = {"phase_idx": 0, "group_idx": 0, "round_idx": 0}
        for pi, phase in enumerate(cassette.phases):
            for gi, group in enumerate(phase.groups):
                rc = rounds_completed(group)
                if rc < group.rounds and not group.skipped:
                    pos = {"phase_idx": pi, "group_idx": gi, "round_idx": rc}
                    break
        save_state(cassette, pos, cassette_path)

    while True:
        try:
            play_cassette(cassette, cassette_path)
            break
        except WorkoutPaused:
            restore_terminal()
            _save_current_position()
            if _say_proc and _say_proc.poll() is None:
                _say_proc.terminate()
            print("\n\nWorkout paused. Progress saved. Type 'fg' to resume.\n")
            # Actually suspend the process
            signal.signal(signal.SIGTSTP, signal.SIG_DFL)
            os.kill(os.getpid(), signal.SIGTSTP)
            # --- execution resumes here after fg ---
            print("Resuming workout...\n")
            continue
        except KeyboardInterrupt:
            restore_terminal()
            _save_current_position()
            if _say_proc and _say_proc.poll() is None:
                _say_proc.terminate()
            print("\n\nWorkout stopped. Progress saved.\n")
            print_log(cassette)
            break


if __name__ == "__main__":
    main()
