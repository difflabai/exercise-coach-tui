# exercise-coach-tui

Supplementary TUI for [ai-health](https://github.com/difflabai/ai-health) — an interactive terminal workout companion with voice coaching (macOS `say`).

ai-health generates your workout plans; this tool walks you through each set with a Rich TUI — rest timers, timed holds, progress tracking, and voice cues.

## Usage

```
pip install rich
```

```
python coach.py workout.txt
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

Uses macOS `say` or Linux `espeak`/`spd-say` for coaching cues. Silently skipped if no TTS is available.

On Linux, install a TTS engine:

```
sudo apt install espeak      # Debian/Ubuntu
sudo dnf install espeak-ng   # Fedora
```
