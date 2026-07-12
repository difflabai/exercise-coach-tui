# Remaining PRs — handoff

State of the improvement plan from `docs/recommendations.md` as of 2026-07-12.

**Update (2026-07-12, later): all four remaining PRs below have landed** — A = #8
(Rep, the ASCII buddy), B = #9 (jump navigation + `--only`), C = #10 (paper cuts:
hold fail/early-end, ETA accounting, learned pace, color-env test hermeticity),
D = #11 (config.toml, JSONL history, session summary, T-5s rest tick, `--quiet`).
473 tests green as of #11. Only the [deferred spec-bump items](#deferred-needs-another-cassette-spec-bump--user-handles-spec-versions)
remain, parked by design. The sections below are kept as written for the record.

## Done (merged)

| PR | What |
|----|------|
| #2 | `docs/recommendations.md` + .gitignore cleanup |
| #3 | Refactor into `exercise_coach/` package: Player state machine with typed events (`events.py`), unified `run_screen` key/render loop (`screens.py`), injectable clock/keys, non-destructive `b` back + `r` redo, §6 bug batch (temp WAV leak, TTS reaping, `--rest` sentinel, Ctrl-C at prompts, lazy tones, XDG state/log location with legacy migration, Ctrl-Z/ISIG fix) |
| #4 | Test suite (hermetic, fake clock/keys, fixtures in `tests/fixtures/`), ruff config, GitHub Actions CI (3.12/3.14, ruff + pytest) |
| #5 | Cassette spec v1.2: `tempo` + `per_side` display fields, version acceptance, `docs/cassette-spec.md`, skill zip refreshed |
| #6 | Volume control (§1): `AudioSettings`, `--volume`/`--mute`, global `-`/`+`/`m` keys, footer 🔊/🔇, PCM scaling for tones + piper (detached scaler stage), espeak `-a`, `say [[volm]]`, persistence in XDG settings.json |

250 tests green as of #6. CI gates every PR.

## Remaining

Work them roughly in this order — buddy and navigation both touch `screens.py`/`ui.py`
and are easiest sequentially.

### PR A — ASCII buddy coach (recommendations §2)

- `exercise_coach/buddy.py`: pure function `buddy_panel(mood, frame_time, speech) -> Panel`.
- Moods map to existing player/screen states: ready/transition, working (rep set),
  holding (timed, tremble more as time runs out), resting (wakes at ~10s left),
  overtime (taps foot, speeds up per nag), celebrating (set/group/session), paused.
- 2–4 frame idle animation; pick frame with `int(time.time() * 2) % len(frames)` —
  `render_layout` already redraws 4×/s. Keep him 5–8 rows; collapse to a one-line face
  in the panel title on narrow terminals (`console.width`).
- The caption line (`tts.py` sets it, `ui.render_layout` shows it) becomes his speech
  bubble — route the caption text into the buddy panel instead of the separate row.
- Encouragement pools per mood, rotated (not random — `Date`-free determinism keeps
  tests simple); streak call-outs can wait for PR C's JSONL history.
- `--no-buddy` flag + persisted setting (reuse the settings.json machinery from
  `audio.py` — consider promoting it to a shared `settings.py` while there).
- No cassette/spec changes. Tests: mood selection per state, frame cycling with a fake
  clock, snapshot of the panel, narrow-width collapse.

### PR B — jump navigation (recommendations §3)

- `j` opens a jump-menu overlay from setup/rest/active screens: every group across all
  phases with status (`✓ done`, `2/3`, `skipped`, `→ current`); arrows or row number to
  select; Enter jumps. **Jumping never clears progress** — `Player.jump_to(pi, gi)`
  already exists; the group resumes at its first incomplete set via `rounds_completed`.
- `→` on a menu row expands to individual sets; Enter on a set plays just that set
  (the spec-free way to do one side of a unilateral exercise).
- After a jumped-to group completes, advance to the next *incomplete* group in cassette
  order (Player.advance already skips completed groups — verify it handles the
  jumped-out-of-order case; there are tests for the linear case in `test_player.py`).
- `r` in the menu = redo a completed group (the existing redo event; keep it the only
  destructive action, confirm before clearing).
- `--only "RDL"` (name substring) / `--only 3` (menu index): play that single group,
  log as partial session, exit.
- Build the overlay on `run_screen` — see how pause_screen recursion works; the menu
  needs its own keymap + a char fall-through for row numbers (get_failure_reps shows
  the digit-entry pattern).
- Tests: menu model (rows/statuses) as pure functions, jump preserves progress,
  set-level jump, --only filtering and its log output.

### PR C — paper cuts (recommendations §6 leftovers)

- Timed holds: `f` records a break-early with actual seconds held (clamp like
  get_failure_reps); Enter ends a hold early counting elapsed seconds. Log line format
  for a failed hold should mirror the rep-failure line.
- ETA: don't charge `(rounds_left - 1) * rest` when mid-round; persist learned
  per-exercise pace (median set duration) in the state/history so ETAs are meaningful
  from set one of the next session.
- Anything else marked §6 that hasn't landed — check the list against git log.

### PR D — extras (recommendations §7)

- Promote settings.json to `~/.config/exercise-coach/config.toml` (volume, buddy,
  preferred piper voice, rest default); flags override; keep reading the old
  settings.json once for migration.
- JSONL session history: append one record per session (meta, per-set actuals,
  duration) next to the log; powers streaks (buddy), pace (ETA), and a future --stats.
- End-of-session summary table: total time, sets done/skipped/failed, vs. last session
  with the same title (needs the JSONL history — do after it).
- Pre-rest-end warning tick at T-5s (`audio.py` tone, gate behind volume settings).
- `--quiet` = `--mute --no-buddy`.

### Deferred (needs another cassette spec bump — user handles spec versions)

See recommendations §8: first-class left/right sides, per-exercise rest, buddy tone
hints, authored hold target ranges.

## Conventions used so far (keep them)

- One branch + squash-merged PR per feature; commit messages and PR bodies end with the
  Claude co-author / generated-with lines; wait for CI (`gh pr checks N --watch`)
  before `gh pr merge N --squash --delete-branch`.
- Tests are hermetic: no TTY (injectable `now`/`read_key` on Player/run_screen), no
  real audio (monkeypatch `shutil.which`/`Popen`; for live pty runs prepend a dir of
  no-op `aplay`/`piper`/`espeak-ng`/`espeak`/`say`/`afplay` shims to PATH), no touching
  the real `~/.local/share/exercise-coach` (conftest isolates XDG paths).
- Each feature ran as a workflow: implement → 2–3 adversarial review lenses (schema'd
  findings, confirmed-only, file:line) → fix-and-verify. The lenses caught real
  regressions every single time — don't skip them.
- `python3 -m pytest -q` and `python3 -m ruff check .` must both be green before
  pushing; a pty-driven end-to-end run (`--rest 1` mini workout) is the standard smoke
  test for anything touching player/screens.
