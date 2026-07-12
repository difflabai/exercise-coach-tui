"""Typed events returned by interactive screens.

Screens return `Event` members instead of magic strings; the player reacts
to them by moving its playhead.
"""

from enum import Enum, auto


class Event(Enum):
    DONE = auto()     # screen finished normally (Enter pressed / timer elapsed)
    SKIP = auto()     # skip the current group
    BACK = auto()     # move the playhead back one group (non-destructive)
    PAUSE = auto()    # pause requested (normally handled inside run_screen)
    SUSPEND = auto()  # suspend to shell (normally raised as WorkoutPaused)
    FAIL = auto()     # rep set failed; collect actual reps next
    REDO = auto()     # explicitly replay a completed group (clears its progress)
    JUMP = auto()     # open the jump menu (recommendations §3)
