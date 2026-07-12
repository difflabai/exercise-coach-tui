"""JSONL session history: one structured record per session (recommendations §7).

history.jsonl sits next to workout_log.txt in the XDG data dir and gets one
JSON line per session: a full session's normal end, a Ctrl-C stop (flagged
partial — it is not a completed session), and an --only partial run. It is
the structured mirror of the prose log — it powers the end-of-session
summary (vs. the previous session with the same title) and leaves the door
open for streaks and a future --stats.

Durations come from the player's injectable clock (not time.time) so they
are testable. Reading is defensive: corrupt or truncated lines are skipped,
never fatal — a bad line can only ever cost its own record.
"""

import json
import math
from datetime import datetime
from pathlib import Path

from .models import Cassette, Group

SCHEMA_VERSION = 1


def history_file() -> Path:
    # Lazy import: state.py's DATA_DIR is patched in tests, read it live.
    from . import state
    return state.DATA_DIR / "history.jsonl"


# ---------------------------------------------------------------------------
# Building and writing records
# ---------------------------------------------------------------------------

def _group_record(group: Group) -> dict:
    return {
        "skipped": group.skipped,
        "exercises": [
            {
                "name": ex.name,
                "sets": [
                    {"target": s.reps, "actual": s.actual_reps,
                     "failure": s.failure, "timed": ex.timed}
                    for s in ex.sets
                ],
            }
            for ex in group.exercises
        ],
    }


def build_record(
    cassette: Cassette,
    duration_seconds: float,
    paces: dict[str, int],
    *,
    partial: bool = False,
    matched_name: str | None = None,
    group_pos: tuple[int, int] | None = None,
    timestamp: str | None = None,
) -> dict:
    """Build one session record (pure — nothing is written).

    `duration_seconds` is the wall clock from player start to end, measured
    on the player's clock. `paces` is the session's per-exercise pace
    medians (player.session_paces()). `partial` marks any record that is not
    a completed full session — an --only run or a Ctrl-C abort — so
    last_session_for_title never uses it as a comparison baseline. A partial
    (--only) run additionally passes `group_pos` so only the played group is
    recorded, mirroring render_partial_log."""
    if group_pos is not None:
        pi, gi = group_pos
        groups = [cassette.phases[pi].groups[gi]]
    else:
        groups = [g for phase in cassette.phases for g in phase.groups]
    return {
        "schema_version": SCHEMA_VERSION,
        "timestamp": timestamp or datetime.now().isoformat(timespec="seconds"),
        "title": cassette.meta.get("title", "Workout"),
        "program": cassette.meta.get("program", ""),
        "partial": partial,
        "matched_name": matched_name,
        "duration_seconds": int(round(duration_seconds)),
        "groups": [_group_record(g) for g in groups],
        "paces": dict(paces),
    }


def append_record(record: dict) -> None:
    """Append one JSON line. Best-effort: never interrupts a workout."""
    try:
        path = history_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(record) + "\n")
    except (OSError, TypeError, ValueError):
        pass


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def read_records() -> list[dict]:
    """Every parseable record, in file (chronological) order.

    Corrupt, truncated, or non-object lines are skipped — a history file
    damaged mid-line must never break the summary or a future --stats."""
    try:
        lines = history_file().read_text().splitlines()
    except OSError:
        return []
    records = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def last_session_for_title(title: str) -> dict | None:
    """The most recent full-session record with this title, or None.

    Partial records (--only runs and Ctrl-C aborts) are ignored: comparing
    a full session against a single-group detour or a cut-short run would
    make the summary deltas nonsense."""
    for record in reversed(read_records()):
        if record.get("title") == title and not record.get("partial"):
            return record
    return None


def _as_list(value) -> list:
    """The value if it is a list, else [] — a hand-edited container of the
    wrong type (null, number, ...) must cost its own contents, not crash."""
    return value if isinstance(value, list) else []


def record_set_counts(record: dict) -> tuple[int, int, int]:
    """(done, skipped, failed) set counts for a record.

    Done = recorded without a reported failure; skipped = never recorded
    (skipped groups, or a session cut short by Ctrl-C). Shape-defensive
    like read_records: a hand-mangled record must not crash the summary."""
    done = skipped = failed = 0
    for group in _as_list(record.get("groups")):
        if not isinstance(group, dict):
            continue
        for ex in _as_list(group.get("exercises")):
            if not isinstance(ex, dict):
                continue
            for s in _as_list(ex.get("sets")):
                if not isinstance(s, dict):
                    continue
                if s.get("failure"):
                    failed += 1
                elif s.get("actual") is not None:
                    done += 1
                else:
                    skipped += 1
    return done, skipped, failed


def record_duration(record: dict) -> int:
    """A record's duration_seconds as an int; 0 for anything malformed."""
    value = record.get("duration_seconds", 0)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return int(value)
    return 0
