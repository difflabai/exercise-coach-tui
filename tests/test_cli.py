"""CLI tests: the try_resume safety gate and main()'s resume-source fallback chain.

All headless and hermetic: ``input()`` is monkeypatched (never a TTY),
``cli.play_cassette`` is replaced with a recorder (nothing ever plays), and
every state/log file lands in tmp_path via conftest's autouse fixtures.
"""

import json
import time
import types

import pytest
from conftest import FakeClock

from exercise_coach import cli
from exercise_coach import state as state_mod
from exercise_coach.cassette import parse_input
from exercise_coach.term import WorkoutPaused

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def cassette_from(raw):
    """Parse a raw cassette dict exactly the way main() does."""
    c, _ = parse_input(json.dumps(raw), 75)
    return c


def write_cassette(tmp_path, raw, name="workout.json"):
    p = tmp_path / name
    p.write_text(json.dumps(raw))
    return p


def save_progress_state(raw, path=None, *, done_sets=1):
    """Save a state file with the first `done_sets` sets of group 1 complete."""
    text = path.read_text() if path is not None else json.dumps(raw)
    c, _ = parse_input(text, 75)
    for s in c.phases[0].groups[0].exercises[0].sets[:done_sets]:
        s.actual_reps = s.reps
    position = {"phase_idx": 0, "group_idx": 0, "round_idx": done_sets}
    state_mod.save_state(c, position, str(path) if path is not None else None)
    return position


def forbid_input(monkeypatch):
    def boom():
        raise AssertionError("input() must not be called on this path")
    monkeypatch.setattr("builtins.input", boom)


@pytest.fixture
def played(monkeypatch):
    """Replace cli.play_cassette with a recorder of (cassette, cassette_path)."""
    calls = []

    def _record(cassette, cassette_path=None, **_kwargs):
        calls.append((cassette, cassette_path))

    monkeypatch.setattr(cli, "play_cassette", _record)
    return calls


def run_main(monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["coach", *argv])
    cli.main()


def first_sets(cassette):
    return [s.actual_reps for s in cassette.phases[0].groups[0].exercises[0].sets]


# ---------------------------------------------------------------------------
# try_resume: the safety gate
# ---------------------------------------------------------------------------


class TestTryResume:
    def test_no_saved_state_returns_none(self, straight_cassette, monkeypatch):
        forbid_input(monkeypatch)
        assert cli.try_resume(cassette_from(straight_cassette()), None) is None

    def test_cassette_hash_mismatch_clears_state(
        self, straight_cassette, tmp_path, monkeypatch, capsys
    ):
        """Progress from one cassette must never be stamped onto another."""
        forbid_input(monkeypatch)
        raw = straight_cassette()
        path = write_cassette(tmp_path, raw)
        save_progress_state(raw, path)
        # The file on disk changes -> saved hash no longer matches.
        path.write_text(json.dumps(straight_cassette(reps=12)))
        fresh = cassette_from(straight_cassette(reps=12))

        assert cli.try_resume(fresh, str(path)) is None

        assert not state_mod.STATE_FILE.exists()  # stale state destroyed
        assert "doesn't match" in capsys.readouterr().err
        assert first_sets(fresh) == [None, None, None]  # nothing applied

    def test_completed_workout_state_is_auto_cleared(
        self, straight_cassette, tmp_path, monkeypatch
    ):
        """A finished workout must not resume (and re-log) forever."""
        forbid_input(monkeypatch)
        raw = straight_cassette()
        path = write_cassette(tmp_path, raw)
        save_progress_state(raw, path, done_sets=3)  # all 3 sets done

        assert cli.try_resume(cassette_from(raw), str(path)) is None
        assert not state_mod.STATE_FILE.exists()

    def test_auto_resume_applies_state_without_prompting(
        self, straight_cassette, tmp_path, monkeypatch
    ):
        forbid_input(monkeypatch)
        raw = straight_cassette()
        path = write_cassette(tmp_path, raw)
        position = save_progress_state(raw, path)
        fresh = cassette_from(raw)

        assert cli.try_resume(fresh, str(path), auto=True) == position

        assert first_sets(fresh) == [10, None, None]
        assert state_mod.STATE_FILE.exists()  # resuming keeps the state file

    def test_stale_state_warns_with_age(self, straight_cassette, monkeypatch, capsys):
        forbid_input(monkeypatch)
        raw = straight_cassette()
        save_progress_state(raw)
        data = json.loads(state_mod.STATE_FILE.read_text())
        data["timestamp"] = time.time() - 5 * 3600  # older than STALE_HOURS
        state_mod.STATE_FILE.write_text(json.dumps(data))

        assert cli.try_resume(cassette_from(raw), None, auto=True) is not None

        err = capsys.readouterr().err
        assert "hours ago" in err
        assert "Saved progress found: 1/3" in err

    @pytest.mark.parametrize("answer", ["y", "Y", "yes", ""])
    def test_interactive_yes_resumes(self, straight_cassette, monkeypatch, answer):
        raw = straight_cassette()
        position = save_progress_state(raw)
        fresh = cassette_from(raw)
        monkeypatch.setattr("builtins.input", lambda: answer)

        assert cli.try_resume(fresh, None) == position
        assert first_sets(fresh) == [10, None, None]

    def test_interactive_no_clears_state(self, straight_cassette, monkeypatch):
        raw = straight_cassette()
        save_progress_state(raw)
        fresh = cassette_from(raw)
        monkeypatch.setattr("builtins.input", lambda: "n")

        assert cli.try_resume(fresh, None) is None

        assert not state_mod.STATE_FILE.exists()
        assert first_sets(fresh) == [None, None, None]

    def test_interactive_eof_defaults_to_resume(self, straight_cassette, monkeypatch):
        """Piped stdin (EOF at the prompt) must resume, not crash or clear."""
        raw = straight_cassette()
        position = save_progress_state(raw)

        def eof():
            raise EOFError

        monkeypatch.setattr("builtins.input", eof)
        assert cli.try_resume(cassette_from(raw), None) == position

    def test_interactive_ctrl_c_aborts_with_130(self, straight_cassette, monkeypatch):
        raw = straight_cassette()
        save_progress_state(raw)

        def interrupt():
            raise KeyboardInterrupt

        monkeypatch.setattr("builtins.input", interrupt)
        with pytest.raises(SystemExit) as exc:
            cli.try_resume(cassette_from(raw), None)
        assert exc.value.code == 130
        assert state_mod.STATE_FILE.exists()  # aborting must not destroy progress


# ---------------------------------------------------------------------------
# main() --resume: cassette source fallback chain
# ---------------------------------------------------------------------------


class TestMainResume:
    def test_no_saved_state_exits_1(self, played, monkeypatch, capsys):
        forbid_input(monkeypatch)
        with pytest.raises(SystemExit) as exc:
            run_main(monkeypatch, "--resume")
        assert exc.value.code == 1
        assert "No saved state to resume" in capsys.readouterr().err
        assert played == []

    def test_file_arg_wins_over_saved_path(
        self, straight_cassette, played, tmp_path, monkeypatch
    ):
        forbid_input(monkeypatch)
        raw = straight_cassette(title="File Arg Wins")
        file_a = write_cassette(tmp_path, raw, "a.json")
        save_progress_state(raw, file_a)
        file_b = write_cassette(tmp_path, raw, "b.json")  # same content, new path

        run_main(monkeypatch, "--resume", str(file_b))

        (cassette, path), = played
        assert path == str(file_b)  # the explicit arg, not the saved path
        assert first_sets(cassette) == [10, None, None]  # progress applied

    def test_saved_path_used_when_no_file_arg(
        self, straight_cassette, played, tmp_path, monkeypatch
    ):
        forbid_input(monkeypatch)
        raw = straight_cassette(title="Saved Path")
        file = write_cassette(tmp_path, raw)
        save_progress_state(raw, file)

        run_main(monkeypatch, "--resume")

        (cassette, path), = played
        assert path == str(file.resolve())
        assert cassette.meta["title"] == "Saved Path"
        assert first_sets(cassette) == [10, None, None]

    def test_embedded_source_used_when_no_path_saved(
        self, straight_cassette, played, monkeypatch
    ):
        forbid_input(monkeypatch)
        raw = straight_cassette(title="From Source")
        save_progress_state(raw)  # no path: only the embedded snapshot

        run_main(monkeypatch, "--resume")

        (cassette, path), = played
        assert path is None
        assert cassette.meta["title"] == "From Source"
        assert first_sets(cassette) == [10, None, None]

    def test_embedded_source_used_when_saved_path_is_gone(
        self, straight_cassette, played, tmp_path, monkeypatch
    ):
        forbid_input(monkeypatch)
        raw = straight_cassette(title="Snapshot Fallback")
        file = write_cassette(tmp_path, raw)
        save_progress_state(raw, file)
        file.unlink()  # stale path on disk -> embedded snapshot must win

        run_main(monkeypatch, "--resume")

        (cassette, path), = played
        assert path is None
        assert cassette.meta["title"] == "Snapshot Fallback"
        assert first_sets(cassette) == [10, None, None]

    def test_no_cassette_anywhere_exits_1(self, played, monkeypatch, capsys):
        forbid_input(monkeypatch)
        state_mod.DATA_DIR.mkdir(parents=True)
        state_mod.STATE_FILE.write_text(json.dumps({
            "timestamp": time.time(),
            "cassette_hash": "",
            "cassette_path": "",
            "cassette_source": "",
            "position": {"phase_idx": 0, "group_idx": 0, "round_idx": 0},
            "groups_state": [],
        }))

        with pytest.raises(SystemExit) as exc:
            run_main(monkeypatch, "--resume")
        assert exc.value.code == 1
        assert "No saved cassette to resume" in capsys.readouterr().err
        assert played == []

    def test_completed_state_starts_fresh(
        self, straight_cassette, played, tmp_path, monkeypatch, capsys
    ):
        forbid_input(monkeypatch)
        raw = straight_cassette()
        file = write_cassette(tmp_path, raw)
        save_progress_state(raw, file, done_sets=3)  # workout already finished

        run_main(monkeypatch, "--resume")

        assert "Nothing to resume; starting fresh." in capsys.readouterr().err
        (cassette, path), = played  # still plays, from scratch
        assert path == str(file.resolve())
        assert first_sets(cassette) == [None, None, None]
        assert not state_mod.STATE_FILE.exists()


# ---------------------------------------------------------------------------
# main() without a file: the resume offer
# ---------------------------------------------------------------------------


class TestMainResumeOffer:
    def test_offer_accepted_plays_saved_cassette(
        self, straight_cassette, played, tmp_path, monkeypatch
    ):
        raw = straight_cassette(title="Offered")
        file = write_cassette(tmp_path, raw)
        save_progress_state(raw, file)
        monkeypatch.setattr("builtins.input", lambda: "y")

        run_main(monkeypatch)  # no file argument at all

        (cassette, path), = played
        assert path == str(file.resolve())
        assert cassette.meta["title"] == "Offered"
        assert first_sets(cassette) == [10, None, None]

    def test_offer_accepted_from_embedded_source(
        self, straight_cassette, played, monkeypatch
    ):
        raw = straight_cassette(title="Offered Source")
        save_progress_state(raw)  # pathless state
        monkeypatch.setattr("builtins.input", lambda: "y")

        run_main(monkeypatch)

        (cassette, path), = played
        assert path is None
        assert cassette.meta["title"] == "Offered Source"

    def test_offer_declined_falls_back_to_pasted_input(
        self, straight_cassette, played, tmp_path, monkeypatch
    ):
        raw = straight_cassette(title="Declined")
        file = write_cassette(tmp_path, raw)
        save_progress_state(raw, file)
        # "n" declines the resume, then the pasted-workout prompt gets a
        # legacy text workout terminated by an empty line.
        answers = iter(["n", "Push-ups 2x8 | bodyweight", ""])
        monkeypatch.setattr("builtins.input", lambda: next(answers))

        run_main(monkeypatch)

        (cassette, path), = played
        assert path is None
        assert cassette.meta["title"] == "Workout"  # the text cassette, not "Declined"
        group = cassette.phases[0].groups[0]
        assert group.exercises[0].name == "Push-ups"
        assert group.rounds == 2
        assert group.rest == 75  # text input default
        assert not state_mod.STATE_FILE.exists()  # declining cleared the state


# ---------------------------------------------------------------------------
# main(): the session wall clock survives pause/resume
# ---------------------------------------------------------------------------


class TestSessionElapsedCarryover:
    """A paused/resumed session's history duration must cover the whole
    workout: main() feeds prior active seconds into play_cassette (and the
    state file) instead of letting each (re-)invocation re-clock from zero."""

    @pytest.fixture
    def recorded_plays(self, monkeypatch):
        """Like `played`, but records the prior_elapsed keyword."""
        seen = []

        def _record(cassette, cassette_path=None, *, prior_elapsed=0.0, **_kw):
            seen.append(prior_elapsed)

        monkeypatch.setattr(cli, "play_cassette", _record)
        return seen

    def test_fresh_run_passes_zero_prior_elapsed(
        self, recorded_plays, straight_cassette, tmp_path, monkeypatch
    ):
        forbid_input(monkeypatch)
        path = write_cassette(tmp_path, straight_cassette())
        run_main(monkeypatch, str(path))
        assert recorded_plays == [0.0]

    def test_resume_feeds_saved_session_elapsed_into_play_cassette(
        self, recorded_plays, straight_cassette, tmp_path, monkeypatch, capsys
    ):
        forbid_input(monkeypatch)
        raw = straight_cassette()
        path = write_cassette(tmp_path, raw)
        c = cassette_from(raw)
        c.phases[0].groups[0].exercises[0].sets[0].actual_reps = 10
        state_mod.save_state(
            c, {"phase_idx": 0, "group_idx": 0, "round_idx": 1}, str(path),
            session_elapsed=345.6,
        )

        run_main(monkeypatch, "--resume")

        assert recorded_plays == [345.6]

    def test_offered_resume_accepted_feeds_saved_elapsed(
        self, recorded_plays, straight_cassette, tmp_path, monkeypatch, capsys
    ):
        raw = straight_cassette()
        path = write_cassette(tmp_path, raw)
        c = cassette_from(raw)
        c.phases[0].groups[0].exercises[0].sets[0].actual_reps = 10
        state_mod.save_state(
            c, {"phase_idx": 0, "group_idx": 0, "round_idx": 1}, str(path),
            session_elapsed=61.5,
        )
        monkeypatch.setattr("builtins.input", lambda: "y")

        run_main(monkeypatch)  # no file arg: the interactive resume offer

        assert recorded_plays == [61.5]

    def test_file_arg_resume_accepted_feeds_saved_elapsed(
        self, recorded_plays, straight_cassette, tmp_path, monkeypatch, capsys
    ):
        raw = straight_cassette()
        path = write_cassette(tmp_path, raw)
        c = cassette_from(raw)
        c.phases[0].groups[0].exercises[0].sets[0].actual_reps = 10
        state_mod.save_state(
            c, {"phase_idx": 0, "group_idx": 0, "round_idx": 1}, str(path),
            session_elapsed=42.0,
        )
        monkeypatch.setattr("builtins.input", lambda: "y")

        run_main(monkeypatch, str(path))  # explicit file + accepted prompt

        assert recorded_plays == [42.0]

    def test_ctrl_z_pause_accumulates_active_time_across_reentry(
        self, straight_cassette, tmp_path, monkeypatch, capsys
    ):
        """The pause loop: 60s of work, Ctrl-Z (suspended 300s), fg, finish.
        The re-invocation must get the pre-pause 60s — and only the 60s of
        *active* time, not the suspension — and the state file saved at the
        pause must carry it too."""
        forbid_input(monkeypatch)
        path = write_cassette(tmp_path, straight_cassette())
        clock = FakeClock()
        monkeypatch.setattr(cli, "time", types.SimpleNamespace(time=clock.now))
        # Suspension must not actually stop the test process; model it as
        # 300s of wall time passing while suspended.
        monkeypatch.setattr(
            cli, "os",
            types.SimpleNamespace(kill=lambda *_a: clock.sleep(300), getpid=lambda: 0),
        )
        monkeypatch.setattr(
            cli, "signal",
            types.SimpleNamespace(signal=lambda *_a: None, SIGTSTP=0, SIG_DFL=0),
        )

        seen = []
        saved_at_pause = []

        def _fake_play(cassette, cassette_path=None, *, prior_elapsed=0.0, **_kw):
            seen.append(prior_elapsed)
            if len(seen) == 1:
                clock.sleep(60)  # 60s of active work, then Ctrl-Z
                raise WorkoutPaused()

        monkeypatch.setattr(cli, "play_cassette", _fake_play)
        real_save_state = state_mod.save_state

        def _spy_save_state(*args, **kwargs):
            saved_at_pause.append(kwargs.get("session_elapsed", 0.0))
            real_save_state(*args, **kwargs)

        monkeypatch.setattr(cli, "save_state", _spy_save_state)

        run_main(monkeypatch, str(path))

        assert seen == [0.0, 60.0]
        assert saved_at_pause == [60.0]


# ---------------------------------------------------------------------------
# main(): --rest override rule and input validation
# ---------------------------------------------------------------------------


class TestMainRestOverride:
    def test_json_cassette_keeps_its_own_rest_without_flag(
        self, straight_cassette, played, tmp_path, monkeypatch
    ):
        forbid_input(monkeypatch)
        file = write_cassette(tmp_path, straight_cassette(rest=90))

        run_main(monkeypatch, str(file))

        (cassette, _), = played
        assert cassette.phases[0].groups[0].rest == 90

    def test_explicit_rest_overrides_json(
        self, straight_cassette, played, tmp_path, monkeypatch
    ):
        forbid_input(monkeypatch)
        file = write_cassette(tmp_path, straight_cassette(rest=90))

        run_main(monkeypatch, str(file), "--rest", "20")

        (cassette, _), = played
        assert cassette.phases[0].groups[0].rest == 20

    def test_rest_75_still_overrides_json(
        self, straight_cassette, played, tmp_path, monkeypatch
    ):
        """--rest 75 equals the text default, but as an explicit flag it must
        override the JSON cassette's own rest (the args.rest-is-None sentinel)."""
        forbid_input(monkeypatch)
        file = write_cassette(tmp_path, straight_cassette(rest=90))

        run_main(monkeypatch, str(file), "--rest", "75")

        (cassette, _), = played
        assert cassette.phases[0].groups[0].rest == 75

    def test_text_input_defaults_to_75(self, played, tmp_path, monkeypatch):
        forbid_input(monkeypatch)
        file = tmp_path / "workout.txt"
        file.write_text("Squats 3x10 | 24kg\n")

        run_main(monkeypatch, str(file))

        (cassette, path), = played
        assert path == str(file)
        assert cassette.phases[0].groups[0].rest == 75

    def test_text_input_honors_rest_flag(self, played, tmp_path, monkeypatch):
        forbid_input(monkeypatch)
        file = tmp_path / "workout.txt"
        file.write_text("Squats 3x10 | 24kg\n")

        run_main(monkeypatch, str(file), "--rest", "30")

        (cassette, _), = played
        assert cassette.phases[0].groups[0].rest == 30

    def test_unparseable_input_exits_1(self, played, tmp_path, monkeypatch, capsys):
        forbid_input(monkeypatch)
        file = tmp_path / "junk.txt"
        file.write_text("no sets or reps here\n")

        with pytest.raises(SystemExit) as exc:
            run_main(monkeypatch, str(file))
        assert exc.value.code == 1
        assert "No exercises parsed" in capsys.readouterr().err
        assert played == []


# ---------------------------------------------------------------------------
# Startup wiring: persisted paces are loaded before playback
# ---------------------------------------------------------------------------


class TestStartupLoadsPaces:
    def test_main_loads_persisted_paces_before_playing(
        self, played, straight_cassette, tmp_path, monkeypatch,
    ):
        """The mutation gate: deleting main()'s user_settings.load_paces()
        call must fail a test — otherwise the persisted paces silently never
        reach the live ETA (build_progress_bar reads the module global)."""
        from exercise_coach import settings as user_settings

        user_settings.update_paces({"Squat": 42})
        user_settings.paces = {}  # simulate a fresh process
        forbid_input(monkeypatch)
        path = write_cassette(tmp_path, straight_cassette())

        run_main(monkeypatch, str(path))

        assert user_settings.paces == {"Squat": 42}
        assert len(played) == 1  # the workout actually started


# ---------------------------------------------------------------------------
# Test-process hermeticity: color-forcing env vars are neutralized
# ---------------------------------------------------------------------------


class TestColorEnvNeutralized:
    """conftest deletes FORCE_COLOR & co. before Rich is imported and keeps
    them out per-test — `FORCE_COLOR=3 pytest` once broke try_resume's capsys
    assertions with ANSI escapes. Assert the neutralization directly (a
    subprocess pytest-in-pytest run would be slow)."""

    def test_color_forcing_vars_absent_from_environ(self):
        import os
        for var in ("FORCE_COLOR", "NO_COLOR", "CLICOLOR_FORCE", "COLORTERM"):
            assert var not in os.environ

    def test_rich_console_emits_no_ansi_to_a_plain_file(self):
        from io import StringIO

        from rich.console import Console

        out = StringIO()
        Console(file=out).print("[red]styled[/red]")
        assert "\x1b[" not in out.getvalue()

    def test_try_resume_output_is_plain_text(self, straight_cassette, monkeypatch, capsys):
        """The exact §-hermeticity victim: try_resume's stderr must be
        ANSI-free under capsys no matter the invoking shell's env."""
        forbid_input(monkeypatch)
        raw = straight_cassette()
        save_progress_state(raw)

        cli.try_resume(cassette_from(raw), None, auto=True)

        err = capsys.readouterr().err
        assert "Saved progress found: 1/3" in err
        assert "\x1b[" not in err
