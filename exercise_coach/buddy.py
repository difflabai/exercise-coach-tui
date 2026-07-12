"""Rep, the ASCII buddy coach (recommendations §2).

A small kaomoji character who lives beside the active panel and mirrors the
player state: waving on setup, pumping a dumbbell during sets, trembling
through holds, dozing through rest, tapping his foot in overtime. The pure
`buddy_panel` renders one frame; everything derives from its arguments —
`frame_time` drives both the idle animation and the encouragement rotation —
so output is reproducible under a fake clock (no random, no datetime.now).

The only module state is the transient celebration window: the player stamps
`celebrate(now)` where the set/group-complete chimes play, and render_layout
overrides the mood to CELEBRATING while `celebrating(now)` holds.
"""

from enum import Enum, auto

from rich.panel import Panel
from rich.text import Text


class Mood(Enum):
    READY = auto()
    WORKING = auto()
    HOLDING = auto()
    RESTING = auto()
    OVERTIME = auto()
    CELEBRATING = auto()
    PAUSED = auto()


TREMBLE_AT = 0.7      # HOLDING: fraction of the hold elapsed that starts the trembling
WAKE_AT = 10.0        # RESTING: seconds of rest left at which Rep wakes up
LINE_PERIOD = 8.0     # seconds each encouragement line stays up before rotating
CELEBRATE_DURATION = 2.0
COLLAPSE_WIDTH = 60   # terminal columns below which the art collapses to a title face

# Idle animation frames per mood, 2-4 each, picked with int(frame_time * 2) %
# len(frames) — render_layout already redraws 4x/s, so 2 fps is free.
_FRAMES: dict[Mood, list[list[str]]] = {
    Mood.READY: [
        [
            r" \(•‿•)  ",
            r"    |>   ",
            r"   /|    ",
            r"   / \   ",
            r"  ~~~~~  ",
        ],
        [
            r"  (•‿•)ﾉ ",
            r"   <|    ",
            r"    |\   ",
            r"   / \   ",
            r"  ~~~~~  ",
        ],
    ],
    Mood.WORKING: [
        [
            r" (ง'-')ง ",
            r" o==|==o ",
            r"    |    ",
            r"   / \   ",
            r"  ~~~~~  ",
        ],
        [
            r" o==|==o ",
            r" (ง'o')ง ",
            r"    |    ",
            r"   / \   ",
            r"  ~~~~~  ",
        ],
    ],
    Mood.HOLDING: [
        [
            r"  (¬_¬)  ",
            r"  /| |\  ",
            r"   | |   ",
            r"   | |   ",
            r"  ~~~~~  ",
        ],
        [
            r"  (¬_¬ ) ",
            r"  /| |\  ",
            r"   | |   ",
            r"   | |   ",
            r"  ~~~~~  ",
        ],
    ],
    Mood.RESTING: [
        [
            r"   zzz   ",
            r" (－ω－)  ",
            r" _/   \_ ",
            r"         ",
            r"  ~~~~~  ",
        ],
        [
            r"   zzZ   ",
            r" (－ω－)  ",
            r" _/   \_ ",
            r"         ",
            r"  ~~~~~  ",
        ],
    ],
    Mood.OVERTIME: [
        [
            r"  (ಠ_ಠ)  ",
            r"   /|\   ",
            r"    |    ",
            r"   /_\   ",
            r"  tap.   ",
        ],
        [
            r"  (ಠ_ಠ)  ",
            r"   /|\   ",
            r"    |    ",
            r"   / \_  ",
            r"   .tap  ",
        ],
    ],
    Mood.CELEBRATING: [
        [
            r" *  ｡  * ",
            r" \(^o^)/ ",
            r"    |    ",
            r"   / \   ",
            r" ﾟ･ * ･ﾟ ",
        ],
        [
            r" ｡  *  ｡ ",
            r" \(^o^)/ ",
            r"    |    ",
            r"   /\    ",
            r" ･ﾟ * ﾟ･ ",
        ],
        [
            r" ･  ﾟ  ･ ",
            r" \(^o^)ﾉ ",
            r"    |    ",
            r"   / \   ",
            r" *  ｡  * ",
        ],
    ],
    Mood.PAUSED: [
        [
            r"  (‑_‑)  ",
            r"   /|\   ",
            r"    |  ▍▍",
            r"   / \   ",
            r"  ~~~~~  ",
        ],
        [
            r"  (‑_‑)ᶻ ",
            r"   /|\   ",
            r"    |  ▍▍",
            r"   / \   ",
            r"  ~~~~~  ",
        ],
    ],
}

# HOLDING with most of the hold elapsed: shaking replaces the stoic stance.
_HOLD_TREMBLE: list[list[str]] = [
    [
        r" ~(>_<)~ ",
        r"  /| |\  ",
        r"   | |   ",
        r"   | |   ",
        r" ~~~~~~~ ",
    ],
    [
        r" ≈(>_<)≈ ",
        r" ~/| |\~ ",
        r"   | |   ",
        r"   | |   ",
        r" ~~~~~~~ ",
    ],
    [
        r" ~(>_<)≈ ",
        r"  /| |\  ",
        r"  ~| |~  ",
        r"   | |   ",
        r" ~~~~~~~ ",
    ],
]

# RESTING with <= WAKE_AT seconds left: up and stretching, no more zzz.
_REST_AWAKE: list[list[str]] = [
    [
        r"  (o_o)! ",
        r"   \|/   ",
        r"    |    ",
        r"   / \   ",
        r"  ~~~~~  ",
    ],
    [
        r"  (o_o)/ ",
        r"   \|    ",
        r"    |    ",
        r"   / \   ",
        r"  ~~~~~  ",
    ],
]

# One-line faces for the narrow-terminal collapse (shown in the panel title).
_MINI_FACE: dict[Mood, str] = {
    Mood.READY: "(•‿•)ﾉ",
    Mood.WORKING: "(ง'-')ง",
    Mood.HOLDING: "(¬_¬)",
    Mood.RESTING: "(－ω－) zzz",
    Mood.OVERTIME: "(ಠ_ಠ)",
    Mood.CELEBRATING: r"\(^o^)/",
    Mood.PAUSED: "(‑_‑)",
}

# Encouragement pools per mood — rotated by frame_time, never random, so a
# daily-use tool cycles through them and tests stay deterministic.
_LINES: dict[Mood, list[str]] = {
    Mood.READY: [
        "Let's get after it!",
        "Ready when you are.",
        "Fresh set, fresh start.",
    ],
    Mood.WORKING: [
        "Make every rep count!",
        "Smooth and strong.",
        "You've got this!",
        "Own the last rep.",
    ],
    Mood.HOLDING: [
        "Breathe. Hold steady.",
        "Long exhale, stay tall.",
        "Don't you dare drop!",
    ],
    Mood.RESTING: [
        "Shake it out.",
        "Good work — recover.",
        "Deep breaths now.",
    ],
    Mood.OVERTIME: [
        "Rest's over, champ.",
        "The weights miss you.",
        "Any day now...",
    ],
    Mood.CELEBRATING: [
        "Nailed it!",
        "That's how it's done!",
        "Textbook. Beautiful.",
    ],
    Mood.PAUSED: [
        "Paused. I'll wait.",
        "Take your time.",
        "Still here when you're back.",
    ],
}


# ---------------------------------------------------------------------------
# Transient celebration window
# ---------------------------------------------------------------------------

_celebrate_until: float = 0.0


def celebrate(now: float, duration: float = CELEBRATE_DURATION) -> None:
    """Open the celebration window: `celebrating` is true for `duration`
    seconds after `now`. The player stamps this where the completion chimes
    play (sound_set_complete / sound_group_complete)."""
    global _celebrate_until
    _celebrate_until = now + duration


def celebrating(now: float) -> bool:
    return now < _celebrate_until


def reset() -> None:
    """Clear module state (the celebration window) — for tests."""
    global _celebrate_until
    _celebrate_until = 0.0


# ---------------------------------------------------------------------------
# Frame selection + rendering
# ---------------------------------------------------------------------------

def _frames(mood: Mood, intensity: float) -> list[list[str]]:
    """Pick the frame set: HOLDING trembles near the end of the hold and
    RESTING wakes up near the end of the rest (intensity semantics are
    documented on buddy_panel)."""
    if mood is Mood.HOLDING and intensity >= TREMBLE_AT:
        return _HOLD_TREMBLE
    if mood is Mood.RESTING and intensity <= WAKE_AT:
        return _REST_AWAKE
    return _FRAMES[mood]


def _frame_index(mood: Mood, frame_time: float, intensity: float, n: int) -> int:
    """2 fps idle animation; OVERTIME foot-taps speed up with each nag."""
    rate = 2 + intensity if mood is Mood.OVERTIME else 2
    return int(frame_time * rate) % n


def _encouragement(mood: Mood, frame_time: float) -> str:
    pool = _LINES[mood]
    return pool[int(frame_time / LINE_PERIOD) % len(pool)]


def buddy_panel(
    mood: Mood, frame_time: float, speech: str = "", *,
    intensity: float = 0.0, width: int = 80,
) -> Panel:
    """Render one frame of Rep's panel. Pure function of its arguments.

    `speech` (the current TTS caption) fills his speech bubble; when nothing
    was said recently he falls back to a rotating encouragement line.
    `intensity` is mood-specific:

    - HOLDING: fraction of the hold elapsed (0-1) — trembles from TREMBLE_AT.
    - RESTING: seconds of rest remaining — wakes up at <= WAKE_AT.
    - OVERTIME: nag count — the foot-tapping speeds up per nag.

    Below `width` COLLAPSE_WIDTH the art collapses to a one-line face in the
    panel title and the body keeps only the speech line.
    """
    line = speech or _encouragement(mood, frame_time)
    bubble = f"“{line}”"

    if width < COLLAPSE_WIDTH:
        return Panel(
            Text(bubble, style="italic", justify="center"),
            title=f"Rep {_MINI_FACE[mood]}", border_style="magenta", expand=True,
        )

    frames = _frames(mood, intensity)
    body = Text(justify="center")
    for row in frames[_frame_index(mood, frame_time, intensity, len(frames))]:
        body.append(row + "\n")
    body.append("\n")
    body.append(bubble, style="italic")
    return Panel(body, title="Rep", border_style="magenta", expand=True)
