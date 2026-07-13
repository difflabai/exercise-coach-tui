# exercise-coach-tui

Supplementary TUI for [ai-health](https://github.com/difflabai/ai-health) — an interactive terminal workout companion with voice coaching (macOS `say`).

[ai-health](https://github.com/difflabai/ai-health) generates your workout plans; this tool walks you through each set with a Rich TUI — rest timers, timed holds, progress tracking, and voice cues.

## Creating a cassette

The coach reads structured JSON "cassette" files that describe a workout session. Use the [`exercise-coach.skill`](https://github.com/difflabai/exercise-coach-tui/blob/master/exercise-coach.skill) to generate a cassette from your workout plan:

1. [Download the skill](https://github.com/difflabai/exercise-coach-tui/raw/master/exercise-coach.skill) and install it in Claude Code, or in claude.ai (Settings → Capabilities → Skills) — the latter is how the companion [ai-health](https://github.com/difflabai/ai-health) flow runs
2. Ask Claude to create a cassette for your workout — it will output JSON (the skill validates it with [`validate.py`](skills/exercise-coach/scripts/validate.py) before presenting it)
3. Run `coach` and paste the cassette JSON into interactive mode

You can use the [ai-health](https://github.com/difflabai/ai-health) project to guide your exercises — it generates personalised workout plans that the skill can convert into cassettes.

The skill's source lives unpacked in [`skills/exercise-coach/`](skills/exercise-coach/); the `.skill` archive is built from it with `python scripts/build_skill.py` (CI fails if the two drift apart).

## Install

```bash
uv tool install git+https://github.com/difflabai/exercise-coach-tui
# or: pipx install git+https://github.com/difflabai/exercise-coach-tui
```

Or run straight from a clone:

```bash
pip install rich
python coach.py workout.json
```

## Usage

```
coach workout.json
```

Or paste interactively:

```
coach
```

### Workout format

```
Exercise Name 3x12 | 55 lbs
Plank 3x40s | BW
```

`<sets>x<reps>` for rep-based, `<sets>x<seconds>s` for timed holds. Optional `| weight` after.

### Options

```
--rest N     Rest seconds between sets. Text workouts default to 75; JSON
             cassettes keep their own rest values unless --rest is passed
             explicitly (an explicit --rest 75 also overrides).
--volume N   Master volume 0-100 for this run (overrides the saved setting)
--mute       Start muted (captions still show everything the voice would say)
--resume     Resume the last workout without prompting
--reset      Discard saved progress and exit
--log        Print current saved progress and exit
```

### Volume

One master volume (default 70%) covers both the chimes and the voice, with a hard mute on top:

- **Keys, on every screen:** `-` / `+` (or `=`) step the volume by 10%, `m` toggles mute — a hard mute that also cuts off any speech or chime already playing. The current level shows at the right of the progress bar (`🔊 70%`, or `🔇` when muted).
- **Flags:** `--volume N` (0-100) and `--mute` set the level for one run without touching the saved setting.
- **Persistence:** key changes are saved to `settings.json` in the data dir (see below), so your level sticks between sessions.

Muting never mutes the experience: captions keep showing every coaching line, so the TUI stays fully usable with sound off.

### Resume

Progress auto-saves on every completed set and on Ctrl-C. Re-run the same workout to pick up where you left off.

### Data files

Saved progress (`.workout_state.json`), the session log (`workout_log.txt`), and audio settings (`settings.json`) live in `~/.local/share/exercise-coach/` (or `$XDG_DATA_HOME/exercise-coach/` if set). Files from older versions that sat next to `coach.py` are moved there automatically on first run.

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

## License

[AGPL-3.0-or-later](LICENSE), matching the rest of the [ai-health](https://github.com/difflabai/ai-health) ecosystem.
