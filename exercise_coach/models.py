"""Cassette data model."""

from dataclasses import dataclass, field


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
    _source: str = ""  # raw input text for saving in state
