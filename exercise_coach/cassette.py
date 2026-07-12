"""Cassette loading, parsing, validation, and pure cassette queries.

Also home of the legacy text data model (`Exercise`) and its parsers,
kept for text-input backward compatibility.
"""

import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

from .models import (
    Cassette,
    ContextExercise,
    ExerciseData,
    Group,
    Phase,
    SetData,
    TimedCue,
)

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

# Schema versions this player fully understands. Unknown (future) versions
# get a one-line stderr warning and best-effort parsing — never a crash.
KNOWN_VERSIONS = ("1.0", "1.1", "1.2")


def load_cassette(path: str) -> Cassette:
    """Load and validate a cassette JSON file."""
    return load_cassette_from_dict(json.loads(Path(path).read_text()))


def load_cassette_from_dict(data: dict, rest_fallback: int = 75) -> Cassette:
    """Build a Cassette from a parsed JSON dict.

    Unknown fields anywhere are silently ignored (forward compat).
    `rest_fallback` applies only when neither the group nor meta.rest_default
    provides a rest (an out-of-spec but tolerated cassette) — the CLI passes
    the persisted rest_default here so the config's promise holds even then.
    """
    version = data.get("version", "1.0")
    if version not in KNOWN_VERSIONS:
        print(
            f"Warning: unknown cassette version {version!r} "
            f"(supported: {', '.join(KNOWN_VERSIONS)}); attempting best-effort playback.",
            file=sys.stderr,
        )

    rest_default = data.get("meta", {}).get("rest_default", rest_fallback)

    phases = []
    for phase_data in data.get("phases", []):
        groups = []
        for g in phase_data.get("groups", []):
            rest = g.get("rest") or rest_default

            rounds = g.get("rounds", 1)
            exercises = []
            for ex in g.get("exercises", []):
                sets = [SetData(reps=s["reps"]) for s in ex.get("sets", [])]
                # If sets not specified, generate from rounds
                if not sets:
                    default_reps = ex.get("reps", 0)
                    sets = [SetData(reps=default_reps) for _ in range(rounds)]
                elif len(sets) < rounds:
                    # An explicit sets list shorter than the group's rounds
                    # would crash playback with an IndexError mid-workout;
                    # pad by repeating the last set's target.
                    sets.extend(
                        SetData(reps=sets[-1].reps) for _ in range(rounds - len(sets))
                    )
                exercises.append(ExerciseData(
                    name=ex["name"], load=ex.get("load", ""),
                    timed=ex.get("timed", False), sets=sets,
                    tempo=ex.get("tempo"),
                    per_side=bool(ex.get("per_side", False)),
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
        version=version,
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
# Pure cassette queries (shared by state, ui, and player)
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
        except KeyboardInterrupt:
            console.print("\nAborted.")
            sys.exit(130)
        if line.strip() == "" and lines:
            break
        lines.append(line)
    return "\n".join(lines)


def parse_input(text: str, rest: int) -> tuple[Cassette, bool]:
    """Parse input text. Returns (cassette, is_json). Tries JSON first, then text format.

    `rest` is used for every text-input group, and as the last-resort
    fallback for JSON groups when neither the group nor meta.rest_default
    names one (so the persisted rest_default reaches such cassettes too)."""
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
            if "phases" in data:
                c = load_cassette_from_dict(data, rest_fallback=rest)
                c._source = text
                return c, True
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
    exercises = parse_workout(text)
    c = text_to_cassette(exercises, rest)
    c._source = text
    return c, False
