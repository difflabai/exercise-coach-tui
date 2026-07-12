# exercise-coach-tui

Supplementary TUI for [ai-health](https://github.com/difflabai/ai-health) — an interactive terminal workout companion with voice coaching (macOS `say`).

[ai-health](https://github.com/difflabai/ai-health) generates your workout plans; this tool walks you through each set with a Rich TUI — rest timers, timed holds, progress tracking, and voice cues.

## Creating a cassette

The coach reads structured JSON "cassette" files that describe a workout session. Use the [`exercise-coach.skill`](https://github.com/difflabai/exercise-coach-tui/blob/master/exercise-coach.skill) to generate a cassette from your workout plan:

1. [Download the skill](https://github.com/difflabai/exercise-coach-tui/raw/master/exercise-coach.skill) and install it in Claude Code
2. Ask Claude to create a cassette for your workout — it will output JSON
3. Run `python coach.py` and paste the cassette JSON into interactive mode

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
--rest N    Rest seconds between sets. Text workouts default to 75; JSON
            cassettes keep their own rest values unless --rest is passed
            explicitly (an explicit --rest 75 also overrides).
--resume    Resume the last workout without prompting
--reset     Discard saved progress and exit
--log       Print current saved progress and exit
```

### Resume

Progress auto-saves on every completed set and on Ctrl-C. Re-run the same workout to pick up where you left off.

### Data files

Saved progress (`.workout_state.json`) and the session log (`workout_log.txt`) live in `~/.local/share/exercise-coach/` (or `$XDG_DATA_HOME/exercise-coach/` if set). Files from older versions that sat next to `coach.py` are moved there automatically on first run.

## Voice

Coaching cues use, in order: macOS `say`, [piper](https://github.com/rhasspy/piper) (if `piper` and `aplay` are on PATH and a voice model is found), then `espeak-ng`/`espeak`. Silently skipped if none are available.

### Piper setup (Linux)

```bash
pip install piper-tts

# Grab the recommended voice (ryan/high — punchier than lessac for coaching cues)
mkdir -p ~/piper-voices && cd ~/piper-voices
wget https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/ryan/high/en_US-ryan-high.onnx
wget https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/ryan/high/en_US-ryan-high.onnx.json

# Smoke-test
echo "the velcro cat approaches" | piper -m ~/piper-voices/en_US-ryan-high.onnx -f out.wav && aplay out.wav
```

The coach prefers `en_US-ryan-high.onnx` if present; otherwise it picks the first `.onnx` it finds in `~/piper-voices/` or `~/.local/share/piper-voices/`. To pin a specific voice, set `PIPER_MODEL=/path/to/voice.onnx`. Browse other voices at [rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices).
