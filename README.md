# exercise-coach-tui

Supplementary TUI for [ai-health](https://github.com/difflabai/ai-health) — an interactive terminal workout companion with voice coaching (macOS `say`).

[ai-health](https://github.com/difflabai/ai-health) generates your workout plans; this tool walks you through each set with a Rich TUI — rest timers, timed holds, progress tracking, and voice cues.

## Creating a cassette

The coach reads structured JSON "cassette" files that describe a workout session. Use the [`exercise-coach.skill`](https://github.com/difflabai/exercise-coach-tui/blob/master/exercise-coach.skill) to generate a cassette from your workout plan:

1. [Download the skill](https://github.com/difflabai/exercise-coach-tui/raw/master/exercise-coach.skill) and install it in Claude Code
2. Ask Claude to create a cassette for your workout — it will produce a JSON session file
3. Feed the cassette to the coach TUI

You can use the [ai-health](https://github.com/difflabai/ai-health) project to guide your exercises — it generates personalised workout plans that the skill can convert into cassettes.

## Usage

```
pip install rich
```

```
python coach.py workout.json
```

Or paste interactively:

```
python coach.py
```

### Workout format

```
Exercise Name 3x12 | 55 lbs
Plank 3x40s | BW
```

`<sets>x<reps>` for rep-based, `<sets>x<seconds>s` for timed holds. Optional `| weight` after.

### Options

```
--rest N    Rest seconds between sets (default: 75)
--reset     Discard saved progress and exit
--log       Print current saved progress and exit
```

### Resume

Progress auto-saves on every completed set and on Ctrl-C. Re-run the same workout to pick up where you left off.

## Voice

Uses macOS `say` for coaching cues. Silently skipped on other platforms.
