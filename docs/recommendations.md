# Recommendations

A review of `coach.py` (currently a single ~1,770-line module) with proposed improvements.
Scope note: the cassette **spec version stays as-is**. Anything that would require a spec
change is flagged and collected in [§8](#8-deferred-needs-a-cassette-spec-change).

---

## 1. Volume control (including full mute)

Today audio is fire-and-forget at fixed loudness: tones are generated at hardcoded
`volume=0.4–0.5` (`_generate_tone`), and TTS goes straight to `say`/piper+`aplay`/espeak
with no level control. There is no way to quiet it besides system volume.

**Proposal: a single master volume (0–100%) plus hard mute, controllable three ways:**

- `--volume N` / `--mute` CLI flags.
- Runtime keys, available on every screen: `-` / `+` step volume by 10%, `m` toggles mute.
  Show the current level in the footer next to the progress bar (`🔊 70%` / `🔇`).
- Persist the last-used level in a small config file (see §7) so it sticks between sessions.

Implementation sketch:

- Introduce an `AudioSettings` object (volume float, muted bool) threaded through — or,
  pragmatically, one module-level settings object in a new `audio.py` (see §4).
- **Tones:** trivial — scale samples at generation time, or pre-generate once at full
  amplitude and scale the PCM buffer on play (a `bytes → bytes` int16 multiply). Skip
  playback entirely when muted or volume is 0.
- **Piper TTS:** the piper→aplay pipeline is raw S16LE PCM, so volume is the same int16
  scale. Insert a tiny scaling step: read piper stdout in a background thread, scale,
  write to aplay stdin. Alternatively pipe through `sox -v {vol}` when available.
- **`say`/espeak fallbacks:** `say` has no volume flag per-utterance (there's `[[volm]]`
  embedded markup on macOS — usable); `espeak-ng` has `-a 0..200`. Where a backend can't
  be scaled, treat volume as a mute threshold (speak at <n% → skip).
- **Mute ≠ silence the experience:** captions already exist as the no-audio channel.
  When muted, extend caption prominence (they become the primary cue) — this mostly
  already works thanks to the 12s caption display.

Also fix while in there: `play_sound()` leaks a `NamedTemporaryFile(delete=False)` per
set with no cleanup (see §6). A persistent per-session temp file (write once per tone,
reuse) removes both the leak and the per-play write.

## 2. Friendliness — "Rep", the ASCII coach buddy

The TUI is functional but sterile: a table, a panel, a progress bar. For a
nearly-every-day tool, a persistent character gives it personality and makes state
legible at a glance.

**Proposal: a small ASCII buddy with moods, living in its own panel beside the
active-exercise panel** (Rich `Table.grid` with two columns handles this). The caption
line becomes his speech bubble, so everything the voice says appears as the buddy
talking — unifying two existing features (TTS + captions) into one character.

Mood states map directly onto the player states that already exist:

| State | Trigger | Sketch (2–4 frame idle animation at the existing 4 fps) |
|---|---|---|
| Ready / transition | setup screen | `(•‿•)ﾉ` waving, "Next up: RDLs!" |
| Working | rep-based set active | `(ง •̀_•́)ง` pumping tiny dumbbell frames |
| Holding | timed hold | `(¬_¬”)` trembling more as time runs out |
| Resting | rest timer | `(－ω－) zzz` → wakes up at 10s left |
| Overtime | rest overtime | `(ಠ_ಠ)` tapping foot, frames speed up per nag |
| Celebrating | set/group/session complete | `\(^o^)/` confetti characters for ~2s |
| Paused | pause screen | `(‑_‑) 💤` |

Design notes:

- Keep him **small** (5–8 rows) so the overview table stays the star. On narrow
  terminals, collapse to a one-line face in the panel title.
- Frame animation is nearly free: `render_layout` already redraws 4×/second; pick the
  frame with `int(time.time() * 2) % len(frames)`.
- Personality through variety: rotate encouragement lines ("Last one, make it count!",
  streak call-outs like "That's 3 sessions this week 🔥" sourced from `workout_log.txt`).
  Keep a pool per mood and cycle — repetition kills charm for a daily-use tool.
- Make him optional (`--no-buddy` and a config key) for focused days.
- Implement as its own module (`buddy.py`) with a pure function
  `buddy_panel(mood, frame_time, speech) -> Panel` — trivially testable.

This needs **no cassette/spec change**: moods derive entirely from player state.

## 3. Navigation — jump to any exercise, reorder on the fly, partial work

Current navigation is strictly linear: `s` skips a whole group, `b` goes back one group
**and destroys progress** in both the current and previous group
(`go_back_to_previous_group` calls `clear_group_progress` on both). There is no way to
do legs before arms, or to run just RDLs, or to redo one side of a unilateral exercise.

**Proposal: a jump menu plus non-destructive movement.**

- **`j` = jump menu**, available from setup/rest/active screens. Overlay listing every
  group across all phases with its status (`✓ done`, `2/3`, `skipped`, `→ current`).
  Navigate with arrows/`k`/`j` or type the row number; Enter jumps there. Jumping
  **never clears progress** — completed sets stay completed; the player simply moves
  the playhead. Returning to a half-done group resumes at its first incomplete set
  (the resume logic in `rounds_completed`/`apply_state` already supports this).
- **Reordering falls out for free:** to do legs first, jump to legs; when the group
  completes, the player advances to the next *incomplete* group in cassette order
  (skipping done ones) instead of blindly `gi += 1`.
- **"Just this one" mode:** `python coach.py workout.json --only "RDL"` (name substring
  match, or `--only 3` by menu index) plays that single group and exits, logging as a
  partial session. Useful for the "I only need one thing today" case.
- **Redo / partial sets:** in the jump menu, `r` on a group offers "redo" (explicitly
  clears that group — this becomes the *only* destructive action, and it's deliberate
  and single-target). `b` becomes a pure playhead move (previous group, no clearing);
  the old clear-on-back behavior goes away.
- **One side of a unilateral exercise:** without touching the spec, treat it as
  set-level granularity — the jump menu can expand a group (`→` key) to show individual
  sets, and Enter on a set plays just that set. Doing "left side only" = do one set of
  the pair and jump away; the state file already tracks per-set completion so nothing
  else is needed. First-class left/right labeling is a spec matter — deferred to §8.

Prerequisite: this is much easier once the player is a state machine with an explicit
playhead (§4) instead of four nested `while` loops with `jump_back`/`skip_group`/
`resuming` flags threaded through them.

## 4. Refactor

`coach.py` mixes data model, parsing, audio synthesis, TTS process management, terminal
control, rendering, persistence, and a 230-line quadruply-nested playback loop. Suggested
package layout (keep `coach.py` as a thin entry point so `python coach.py` still works):

```
coach.py                  # entry point → cli.main()
exercise_coach/
    models.py             # Cassette/Phase/Group/ExerciseData/SetData (+ legacy Exercise)
    cassette.py           # load/parse/validate, text→cassette, hashing
    audio.py              # tone generation, play_sound, AudioSettings (volume/mute)
    tts.py                # piper/say/espeak backends, captions, proc lifecycle
    term.py               # cbreak, read_key, drain_stdin
    state.py              # save/load/apply state, log rendering/append
    ui.py                 # build_overview, panels, progress bar, render_layout
    buddy.py              # §2
    player.py             # the state machine
    cli.py                # argparse, resume flow
```

Key structural changes, in order of value:

1. **Player state machine.** Replace `play_cassette`'s nested loops with a `Player`
   holding an explicit playhead (`phase_idx, group_idx, round_idx, ex_idx`) and methods
   `advance()`, `jump_to(pi, gi)`, `skip_group()`, `previous()`. Screens return typed
   events (an `Enum`: `DONE`, `SKIP`, `BACK`, `JUMP`, `PAUSE`) instead of magic strings
   (`"skip_group"`, `"go_back"`, `"done"`). This kills the `jump_back`/`resuming` flag
   plumbing and is the enabler for §3.
2. **One key-input screen loop.** Six near-identical blocks
   (`enter_cbreak → drain → while: render, read_key, sleep(0.25) → restore`) exist in
   `pause_screen`, `transition_screen`, `rest_timer`, `timed_hold`, `get_failure_reps`,
   and the rep-set loop. Extract a single
   `run_screen(render_fn, keymap, tick=None) -> Event` helper; each screen becomes its
   render function plus a key→event dict. Global keys (`p`, `m`, `+`/`-`, `j`, Ctrl-Z)
   get handled in one place instead of being re-implemented (and currently, unevenly
   available) per screen.
3. **Injectable clock and key source.** `Player` and `run_screen` take `now()` and
   `read_key()` callables (defaulting to `time.time`/real input). This is what makes the
   whole thing testable without a TTY (§5).
4. **Kill or quarantine the legacy model.** `Exercise.log_str` is dead code; the legacy
   text path only needs `parse_workout → text_to_cassette`. Fold into `cassette.py`.
5. **No module-level side effects.** The three tones are synthesized at import time
   (`_SOUND_* = _generate…()`), which slows every `--log`/`--reset` invocation. Make
   them lazy (`functools.lru_cache`).
6. **State/log location.** `.workout_state.json` and `workout_log.txt` live next to the
   script — inside the git repo (`workout_log.txt` is untracked right now). Move to
   platformdirs-style paths (`~/.local/share/exercise-coach/`), keep a fallback read of
   the old location for migration, and gitignore the old names.

## 5. Testing

Nothing is tested today; there's not even a `tests/` dir. After (or alongside) §4:

- **Tooling:** `pytest`, plus `ruff` and `pyright`/`mypy` (`SetData.reps` doubling as
  "reps or seconds" is exactly the kind of thing typing pressure surfaces). Wire into a
  GitHub Actions workflow — this repo already lives on GitHub.
- **Pure-function unit tests** (writable immediately, no refactor needed):
  `parse_exercise`/`parse_workout` (formats, `[completed]` markers, malformed lines),
  `load_cassette_from_dict` (defaults, missing keys, sets-from-rounds generation),
  `rounds_completed`, `count_sets`, `estimate_remaining`, `format_eta`,
  `format_exercise_log`/`render_log`, and a **save→load→apply state round-trip**
  property: applying saved state to a fresh cassette reproduces per-set progress.
- **Player tests** (after §4.1/4.3): drive `Player` with a scripted key source and fake
  clock — "skip mid-round leaves later groups untouched", "jump preserves progress",
  "resume lands on first incomplete set", "back from first group is a no-op".
- **Rendering smoke tests:** Rich supports capture
  (`Console(record=True)` / `console.export_text()`); snapshot the overview table for a
  known cassette so layout regressions are visible in diffs.
- **Cassette fixtures:** check in 2–3 sample cassettes (straight sets, superset, timed
  holds + cues) under `tests/fixtures/`. These double as documentation, since the spec
  currently only exists inside the `.skill` zip.
- **Regression tests for §6 bugs** as each is fixed.

## 6. Bugs and paper cuts found during review

- **Temp file leak:** `play_sound` writes `NamedTemporaryFile(delete=False)` and never
  deletes it — one orphaned WAV in `/tmp` per set/round/rest, forever (`coach.py:354`).
- **Zombie processes:** TTS `Popen`s are never `wait()`ed; `_terminate_say` terminates
  without reaping, and finished `say` processes sit as zombies for the session's
  duration. A `_reap()` pass (`poll()` + drop finished) each `say()` call fixes it.
- **`--rest 75` is ignored for JSON cassettes:** the override heuristic is
  `not is_json or args.rest != 75` (`coach.py:1723`), so explicitly requesting the
  default value can't override a cassette. Use `default=None` in argparse and test for
  "flag was provided".
- **`b` (back) destroys progress** in both current and previous groups — surprising and
  irreversible mid-workout. Superseded by §3's non-destructive playhead moves.
- **Timed holds are rigid:** no `f`/fail path (can't record a hold you broke early), and
  no way to end a hold early and count actual seconds; `enter` does nothing during one.
- **Ctrl-C at the resume prompt** (`try_resume`'s `input()`) tracebacks — the
  `KeyboardInterrupt` handler only wraps `play_cassette`.
- **ETA quirks:** `estimate_remaining` charges `(rounds_left - 1) * rest` even when
  you're mid-round about to rest, and the 30s default per rep-set only adapts within a
  session. Persisting the learned per-exercise pace in the state/log would make ETAs
  meaningful from set one.
- **`skipped` groups and the log:** a skipped group logs `n[0]×…` identically to a
  never-started one; consider an explicit `(skipped)` marker.
- **`workout_log.txt` and `worktrees/` are untracked clutter** in the repo root — add
  both to `.gitignore` (log file moves anyway per §4.6).
- **Caption/say globals** (`_caption`, `_say_procs`, `_old_term`) make the module
  unsafe to reuse and awkward to test — folded into `tts.py`/`term.py` objects in §4.

## 7. Other ideas (smaller, independent)

- **Config file** `~/.config/exercise-coach/config.toml` for volume, buddy on/off,
  preferred piper voice, rest default — the CLI flags override it. Removes the growing
  pile of env vars/flags a daily tool accumulates.
- **Structured history:** `workout_log.txt` is append-only prose. Also append a JSONL
  record per session (cassette meta, per-set actuals, duration). Cheap now, and it
  unlocks streaks (§2), pace learning (§6), and a future `coach.py --stats` (PRs,
  volume per week, adherence).
- **Pre-rest-end warning:** a soft tick at T-5s so you're back on the bench *before*
  the overtime nags start.
- **`--quiet` day mode** = `--mute --no-buddy` in one flag.
- **End-of-session summary screen:** total time, sets done/skipped/failed, vs. last
  time for the same title — one Rich table before exit, data already in hand.

## 8. Deferred — needs a cassette spec change

Parking these per the decision to leave the spec version alone; listed so they're not lost:

- **First-class unilateral exercises:** a `side: left|right` or `unilateral: true`
  field so sets render as "RDL (L)" / "RDL (R)" and the jump menu can target one side
  by name. (§3's set-level jumping is the spec-free workaround.)
- **Per-exercise rest** (currently rest is per-group only).
- **Buddy voice/mood hints in cassettes** (e.g. `tone: "hype" | "calm"` per group) if
  Rep's personality should be authorable per workout.
- **Recording actual hold seconds** for timed sets would fit the current state file,
  but *authoring* target ranges (e.g. "30–45s") is spec territory.
