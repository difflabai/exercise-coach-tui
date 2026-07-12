"""Tests for exercise_coach.state: persistence round-trip, legacy migration, log rendering."""

import hashlib
import json

from exercise_coach import state
from exercise_coach.cassette import all_groups, load_cassette_from_dict
from exercise_coach.models import ExerciseData, Group, SetData


def _progress_snapshot(cassette):
    """Observable per-set progress: (skipped, [[(actual_reps, failure), ...], ...]) per group."""
    return [
        (g.skipped, [[(s.actual_reps, s.failure) for s in ex.sets] for ex in g.exercises])
        for _, _, g in all_groups(cassette)
    ]


def _mark_progress(cassette):
    """Stamp a mixed bag of progress onto the straight fixture cassette."""
    groups = {(pi, gi): g for pi, gi, g in all_groups(cassette)}
    # Warmup fully done.
    for s in groups[(0, 0)].exercises[0].sets:
        s.actual_reps = 20
    # Goblet squats: one clean set, then a failure mid-set-2, set 3 untouched.
    squat = groups[(1, 0)].exercises[0]
    squat.sets[0].actual_reps = 10
    squat.sets[1].actual_reps = 6
    squat.sets[1].failure = True
    # Push-ups skipped entirely.
    groups[(1, 1)].skipped = True


# ---------------------------------------------------------------------------
# save_state / load_state_data / apply_state
# ---------------------------------------------------------------------------

class TestStateRoundTrip:
    def test_round_trip_reproduces_per_set_progress(self, fixture_cassette):
        cassette = fixture_cassette("straight")
        _mark_progress(cassette)
        position = {"phase_idx": 1, "group_idx": 1, "round_idx": 0}

        state.save_state(cassette, position)

        fresh = fixture_cassette("straight")
        assert _progress_snapshot(fresh) != _progress_snapshot(cassette)  # sanity

        loaded = state.load_state_data()
        assert loaded is not None
        resume_pos = state.apply_state(fresh, loaded)

        assert resume_pos == position
        assert _progress_snapshot(fresh) == _progress_snapshot(cassette)

    def test_round_trip_preserves_failure_flags_exactly(self, fixture_cassette):
        cassette = fixture_cassette("straight")
        _mark_progress(cassette)
        state.save_state(cassette, {"phase_idx": 1, "group_idx": 0, "round_idx": 1})

        fresh = fixture_cassette("straight")
        state.apply_state(fresh, state.load_state_data())

        squat = fresh.phases[1].groups[0].exercises[0]
        assert [(s.actual_reps, s.failure) for s in squat.sets] == [
            (10, False), (6, True), (None, False),
        ]
        assert fresh.phases[1].groups[1].skipped is True
        assert fresh.phases[0].groups[0].skipped is False

    def test_save_state_with_cassette_path_records_hash_path_source(
        self, fixture_cassette, tmp_path
    ):
        raw = fixture_cassette("straight", raw=True)
        cassette_file = tmp_path / "straight.json"
        cassette_file.write_text(json.dumps(raw))
        cassette = load_cassette_from_dict(raw)

        state.save_state(cassette, {"phase_idx": 0, "group_idx": 0, "round_idx": 0},
                         str(cassette_file))

        saved = json.loads(state.STATE_FILE.read_text())
        assert saved["cassette_hash"] == hashlib.sha256(cassette_file.read_bytes()).hexdigest()
        assert saved["cassette_path"] == str(cassette_file.resolve())
        assert saved["cassette_source"] == cassette_file.read_text()

    def test_save_state_without_path_uses_in_memory_source(self, fixture_cassette):
        cassette = fixture_cassette("straight")
        cassette._source = '{"the": "original text"}'

        state.save_state(cassette, {"phase_idx": 0, "group_idx": 0, "round_idx": 0})

        saved = json.loads(state.STATE_FILE.read_text())
        assert saved["cassette_hash"] == ""
        assert saved["cassette_path"] == ""
        assert saved["cassette_source"] == '{"the": "original text"}'

    def test_save_state_creates_data_dir(self, fixture_cassette):
        assert not state.DATA_DIR.exists()
        state.save_state(fixture_cassette("straight"), {"phase_idx": 0})
        assert state.STATE_FILE.exists()

    def test_load_state_data_returns_none_when_no_file(self):
        assert state.load_state_data() is None

    def test_load_state_data_returns_none_on_corrupt_json(self):
        state.DATA_DIR.mkdir(parents=True)
        state.STATE_FILE.write_text("{not valid json")
        assert state.load_state_data() is None

    def test_apply_state_defaults_position_when_missing(self, fixture_cassette):
        pos = state.apply_state(fixture_cassette("straight"), {"groups_state": []})
        assert pos == {"phase_idx": 0, "group_idx": 0, "round_idx": 0}

    def test_apply_state_tolerates_mismatched_state(self, fixture_cassette):
        """Extra groups/exercises/sets in the saved state must not crash apply."""
        fresh = fixture_cassette("straight")
        state_dict = {
            "position": {"phase_idx": 0, "group_idx": 0, "round_idx": 0},
            "groups_state": [
                {"phase_idx": 99, "group_idx": 0, "skipped": True, "sets": [[]]},
                {
                    "phase_idx": 0,
                    "group_idx": 0,
                    "skipped": False,
                    # more sets than the cassette has, plus a phantom exercise
                    "sets": [
                        [{"actual_reps": 20, "failure": False}] * 5,
                        [{"actual_reps": 1, "failure": True}],
                    ],
                },
            ],
        }
        state.apply_state(fresh, state_dict)
        warmup = fresh.phases[0].groups[0].exercises[0]
        assert [s.actual_reps for s in warmup.sets] == [20, 20]


class TestClearState:
    def test_clear_state_removes_file(self, fixture_cassette):
        state.save_state(fixture_cassette("straight"), {"phase_idx": 0})
        assert state.STATE_FILE.exists()
        state.clear_state()
        assert not state.STATE_FILE.exists()

    def test_clear_state_noop_when_no_file(self):
        state.clear_state()  # must not raise
        assert not state.STATE_FILE.exists()


# ---------------------------------------------------------------------------
# Legacy file migration
# ---------------------------------------------------------------------------

class TestLegacyMigration:
    def test_moves_both_legacy_files(self):
        legacy = state._LEGACY_DIR
        legacy.mkdir(parents=True)
        (legacy / state.STATE_FILE.name).write_text('{"old": "state"}')
        (legacy / state.LOG_FILE.name).write_text("old log\n")

        state.migrate_legacy_files()

        assert state.STATE_FILE.read_text() == '{"old": "state"}'
        assert state.LOG_FILE.read_text() == "old log\n"
        assert not (legacy / state.STATE_FILE.name).exists()
        assert not (legacy / state.LOG_FILE.name).exists()

    def test_moves_only_the_file_that_exists(self):
        legacy = state._LEGACY_DIR
        legacy.mkdir(parents=True)
        (legacy / state.LOG_FILE.name).write_text("just the log\n")

        state.migrate_legacy_files()

        assert state.LOG_FILE.read_text() == "just the log\n"
        assert not state.STATE_FILE.exists()

    def test_does_not_overwrite_existing_new_file(self):
        legacy = state._LEGACY_DIR
        legacy.mkdir(parents=True)
        (legacy / state.STATE_FILE.name).write_text("legacy contents")
        state.DATA_DIR.mkdir(parents=True)
        state.STATE_FILE.write_text("new contents")

        state.migrate_legacy_files()

        assert state.STATE_FILE.read_text() == "new contents"
        assert (legacy / state.STATE_FILE.name).read_text() == "legacy contents"

    def test_unwritable_destination_warns_but_does_not_crash(self, capsys):
        legacy = state._LEGACY_DIR
        legacy.mkdir(parents=True)
        (legacy / state.LOG_FILE.name).write_text("stuck log\n")
        # A plain file where DATA_DIR should be makes mkdir raise OSError.
        state.DATA_DIR.parent.mkdir(parents=True, exist_ok=True)
        state.DATA_DIR.write_text("not a directory")

        state.migrate_legacy_files()  # must not raise

        assert "could not migrate" in capsys.readouterr().err
        # Legacy file left in place for a later retry.
        assert (legacy / state.LOG_FILE.name).read_text() == "stuck log\n"

    def test_noop_when_nothing_to_migrate(self):
        state._LEGACY_DIR.mkdir(parents=True)
        state.migrate_legacy_files()
        assert not state.STATE_FILE.exists()
        assert not state.LOG_FILE.exists()


# ---------------------------------------------------------------------------
# format_exercise_log / render_log
# ---------------------------------------------------------------------------

def _rep_group(*, name="Bench Press", load="60kg", rounds=3, reps=10, timed=False):
    ex = ExerciseData(
        name=name, load=load, timed=timed,
        sets=[SetData(reps=reps) for _ in range(rounds)],
    )
    return ex, Group(type="straight", rounds=rounds, rest=60, exercises=[ex])


class TestFormatExerciseLog:
    def test_all_sets_complete(self):
        ex, group = _rep_group()
        for s in ex.sets:
            s.actual_reps = 10
        assert state.format_exercise_log(ex, group) == f"{'Bench Press':<25} 3×10 | 60kg"

    def test_all_complete_without_load_has_no_suffix(self):
        ex, group = _rep_group(load="")
        for s in ex.sets:
            s.actual_reps = 10
        line = state.format_exercise_log(ex, group)
        assert line == f"{'Bench Press':<25} 3×10"

    def test_failure_line_reports_reps_and_set_number(self):
        ex, group = _rep_group()
        ex.sets[0].actual_reps = 10
        ex.sets[1].actual_reps = 6
        ex.sets[1].failure = True
        assert state.format_exercise_log(ex, group) == (
            f"{'Bench Press':<25} 3[1]×10 - failed at 6 on set 2 | 60kg"
        )

    def test_failure_with_no_recorded_reps_reports_zero(self):
        ex, group = _rep_group()
        ex.sets[0].failure = True
        line = state.format_exercise_log(ex, group)
        assert "failed at 0 on set 1" in line

    def test_partial_progress_shows_completed_count(self):
        ex, group = _rep_group()
        ex.sets[0].actual_reps = 10
        assert state.format_exercise_log(ex, group) == f"{'Bench Press':<25} 3[1]×10 | 60kg"

    def test_untouched_exercise_shows_zero_completed(self):
        ex, group = _rep_group()
        assert "3[0]×10" in state.format_exercise_log(ex, group)

    def test_skipped_group_marker(self):
        ex, group = _rep_group()
        ex.sets[0].actual_reps = 10  # progress is irrelevant once skipped
        group.skipped = True
        assert state.format_exercise_log(ex, group) == f"{'Bench Press':<25} (skipped) | 60kg"

    def test_timed_exercise_uses_seconds_suffix(self):
        ex, group = _rep_group(name="Plank", load="", rounds=2, reps=30, timed=True)
        for s in ex.sets:
            s.actual_reps = 30
        assert state.format_exercise_log(ex, group) == f"{'Plank':<25} 2×30s"


class TestRenderLog:
    def test_renders_context_then_every_exercise(self, fixture_cassette):
        cassette = fixture_cassette("straight")
        _mark_progress(cassette)
        lines = state.render_log(cassette).splitlines()

        assert lines[0] == f"{'Dead Hangs':<25} — (see notes)"
        assert lines[1] == f"{'Jumping Jacks':<25} 2×20"
        assert lines[2].startswith(f"{'Goblet Squat':<25} 3[1]×10 - failed at 6 on set 2")
        assert lines[3] == f"{'Push-up':<25} (skipped)"
        assert len(lines) == 4


# ---------------------------------------------------------------------------
# save_log
# ---------------------------------------------------------------------------

class TestSaveLog:
    def test_appends_header_and_body(self, fixture_cassette):
        cassette = fixture_cassette("straight")
        _mark_progress(cassette)

        state.save_log(cassette)

        content = state.LOG_FILE.read_text()
        first_line = content.splitlines()[0]
        assert first_line.startswith("--- Straight Sets Day (Test Program) | ")
        assert first_line.endswith(" ---")
        assert state.render_log(cassette) in content
        assert content.endswith("\n\n")

    def test_second_save_appends_not_overwrites(self, fixture_cassette):
        cassette = fixture_cassette("straight")
        state.save_log(cassette)
        first = state.LOG_FILE.read_text()
        state.save_log(cassette)
        second = state.LOG_FILE.read_text()

        assert second.startswith(first)
        assert second.count("--- Straight Sets Day") == 2

    def test_defaults_title_and_omits_empty_program(self, straight_cassette):
        raw = straight_cassette()
        raw["meta"] = {}  # no title, no program
        cassette = load_cassette_from_dict(raw)

        state.save_log(cassette)

        header = state.LOG_FILE.read_text().splitlines()[0]
        assert header.startswith("--- Workout | ")
        assert "(" not in header
