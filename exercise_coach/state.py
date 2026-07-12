"""State persistence and log output."""

import json
import time
from pathlib import Path

from .cassette import all_groups, cassette_content_hash, rounds_completed
from .models import Cassette, ExerciseData, Group

# State/log files live next to coach.py (the repo root), one level above this package.
STATE_FILE = Path(__file__).resolve().parent.parent / ".workout_state.json"
LOG_FILE = Path(__file__).resolve().parent.parent / "workout_log.txt"


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

    # Save cassette source so state is self-contained for resume
    cassette_source = ""
    if cassette_path:
        p = Path(cassette_path)
        if p.exists():
            cassette_source = p.read_text()
    if not cassette_source:
        cassette_source = cassette._source

    data = {
        "timestamp": time.time(),
        "cassette_hash": cassette_content_hash(cassette_path) if cassette_path else "",
        "cassette_path": str(Path(cassette_path).resolve()) if cassette_path else "",
        "cassette_source": cassette_source,
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


def save_log(cassette: Cassette) -> None:
    """Append a timestamped workout log entry to the log file."""
    from datetime import datetime
    title = cassette.meta.get("title", "Workout")
    program = cassette.meta.get("program", "")
    header = f"--- {title}"
    if program:
        header += f" ({program})"
    header += f" | {datetime.now().strftime('%Y-%m-%d %H:%M')} ---"
    entry = header + "\n" + render_log(cassette) + "\n\n"
    with open(LOG_FILE, "a") as f:
        f.write(entry)
