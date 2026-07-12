"""Tests for exercise_coach.cassette: legacy text parsing, cassette loading,
text->cassette conversion, input-format detection, and content hashing."""

import hashlib
import json

import pytest

from exercise_coach.cassette import (
    cassette_content_hash,
    load_cassette_from_dict,
    parse_exercise,
    parse_input,
    parse_workout,
    text_to_cassette,
)
from exercise_coach.models import Cassette

# ---------------------------------------------------------------------------
# parse_exercise
# ---------------------------------------------------------------------------


class TestParseExercise:
    def test_full_line_with_weight_and_phase(self):
        ex = parse_exercise("Goblet Squat 3x10 | 24kg | main")
        assert ex is not None
        assert ex.name == "Goblet Squat"
        assert ex.total_sets == 3
        assert ex.reps == 10
        assert ex.timed is False
        assert ex.weight == "24kg"
        assert ex.phase == "main"
        assert ex.completed_sets == 0
        assert ex.done is False

    def test_name_and_sets_only(self):
        ex = parse_exercise("Push-up 3x15")
        assert ex.name == "Push-up"
        assert ex.weight == ""
        assert ex.phase == ""

    def test_weight_but_no_phase(self):
        ex = parse_exercise("Deadlift 5x5 | 100kg")
        assert ex.weight == "100kg"
        assert ex.phase == ""

    def test_unicode_multiplication_sign(self):
        ex = parse_exercise("Row 4×8")
        assert (ex.total_sets, ex.reps) == (4, 8)

    def test_timed_suffix_sets_timed_flag(self):
        ex = parse_exercise("Plank 3x30s")
        assert ex.timed is True
        assert ex.reps == 30  # seconds stored in reps

    def test_completed_marker(self):
        ex = parse_exercise("Squat 5[2]x8")
        assert ex.total_sets == 5
        assert ex.completed_sets == 2
        assert ex.reps == 8
        assert ex.done is False

    def test_completed_marker_all_done(self):
        ex = parse_exercise("Squat 3[3]x8")
        assert ex.done is True

    def test_surrounding_whitespace_stripped(self):
        ex = parse_exercise("   Curl 2x12 |  15kg  |  arms  ")
        assert ex.name == "Curl"
        assert ex.weight == "15kg"
        assert ex.phase == "arms"

    @pytest.mark.parametrize(
        "line",
        [
            "",
            "   ",
            "just a name",
            "no set spec here | 20kg",
            "x10",  # sets count missing
            "3x",  # reps missing
        ],
    )
    def test_malformed_lines_return_none(self, line):
        assert parse_exercise(line) is None


# ---------------------------------------------------------------------------
# parse_workout
# ---------------------------------------------------------------------------


class TestParseWorkout:
    def test_parses_multiple_lines_in_order(self):
        text = "Squat 3x10 | 60kg\nBench 3x8 | 40kg\nPlank 2x30s"
        exercises = parse_workout(text)
        assert [e.name for e in exercises] == ["Squat", "Bench", "Plank"]
        assert exercises[2].timed is True

    def test_skips_blank_and_malformed_lines(self):
        text = "\nSquat 3x10\n\nnot an exercise\nBench 2x8\n"
        exercises = parse_workout(text)
        assert [e.name for e in exercises] == ["Squat", "Bench"]

    def test_empty_text_gives_empty_list(self):
        assert parse_workout("") == []


# ---------------------------------------------------------------------------
# load_cassette_from_dict
# ---------------------------------------------------------------------------


class TestLoadCassetteFromDict:
    def test_minimal_dict_defaults(self):
        c = load_cassette_from_dict({})
        assert isinstance(c, Cassette)
        assert c.version == "1.0"
        assert c.meta == {}
        assert c.phases == []
        assert c.context_exercises == []
        assert c.voice_session_intro is None
        assert c.voice_session_complete is None

    def test_group_and_exercise_defaults(self):
        data = {
            "phases": [
                {"groups": [{"exercises": [{"name": "Squat", "reps": 10}]}]}
            ]
        }
        c = load_cassette_from_dict(data)
        phase = c.phases[0]
        assert phase.type == "main"
        assert phase.voice_intro is None
        g = phase.groups[0]
        assert g.type == "straight"
        assert g.rounds == 1
        assert g.rest == 75  # falls back to default rest_default of 75
        assert g.voice_intro is None
        assert g.voice_round_complete == []
        assert g.voice_group_complete is None
        assert g.voice_during_set == []
        assert g.setup is None
        assert g.skipped is False
        ex = g.exercises[0]
        assert ex.load == ""
        assert ex.timed is False

    def test_sets_generated_from_rounds(self):
        data = {
            "phases": [
                {
                    "groups": [
                        {"rounds": 4, "exercises": [{"name": "Row", "reps": 12}]}
                    ]
                }
            ]
        }
        c = load_cassette_from_dict(data)
        sets = c.phases[0].groups[0].exercises[0].sets
        assert [s.reps for s in sets] == [12, 12, 12, 12]
        assert all(s.actual_reps is None and s.failure is False for s in sets)

    def test_explicit_sets_override_reps(self):
        data = {
            "phases": [
                {
                    "groups": [
                        {
                            "rounds": 3,
                            "exercises": [
                                {
                                    "name": "Squat",
                                    "reps": 99,
                                    "sets": [{"reps": 10}, {"reps": 10}, {"reps": 8}],
                                }
                            ],
                        }
                    ]
                }
            ]
        }
        sets = load_cassette_from_dict(data).phases[0].groups[0].exercises[0].sets
        assert [s.reps for s in sets] == [10, 10, 8]

    def test_short_sets_list_padded_to_rounds_with_last_target(self):
        data = {
            "phases": [
                {
                    "groups": [
                        {
                            "rounds": 4,
                            "exercises": [
                                {"name": "Squat", "sets": [{"reps": 10}, {"reps": 8}]}
                            ],
                        }
                    ]
                }
            ]
        }
        sets = load_cassette_from_dict(data).phases[0].groups[0].exercises[0].sets
        assert [s.reps for s in sets] == [10, 8, 8, 8]

    def test_missing_reps_and_no_sets_gives_zero_rep_sets(self):
        data = {"phases": [{"groups": [{"rounds": 2, "exercises": [{"name": "X"}]}]}]}
        sets = load_cassette_from_dict(data).phases[0].groups[0].exercises[0].sets
        assert [s.reps for s in sets] == [0, 0]

    def test_group_rest_falls_back_to_meta_rest_default(self):
        data = {
            "meta": {"rest_default": 42},
            "phases": [
                {
                    "groups": [
                        {"exercises": [{"name": "A", "reps": 5}]},
                        {"rest": 90, "exercises": [{"name": "B", "reps": 5}]},
                    ]
                }
            ],
        }
        groups = load_cassette_from_dict(data).phases[0].groups
        assert groups[0].rest == 42
        assert groups[1].rest == 90

    def test_voice_during_set_parsed_as_timed_cues_per_round(self):
        data = {
            "phases": [
                {
                    "groups": [
                        {
                            "rounds": 2,
                            "exercises": [{"name": "Plank", "timed": True, "reps": 30}],
                            "voice_during_set": [
                                [
                                    {"at_seconds": 10, "line": "Halfway."},
                                    {"at_seconds": 25, "line": "Five left."},
                                ],
                                [{"at_seconds": 15, "line": "Hold."}],
                            ],
                        }
                    ]
                }
            ]
        }
        cues = load_cassette_from_dict(data).phases[0].groups[0].voice_during_set
        assert [(c.at_seconds, c.line) for c in cues[0]] == [
            (10, "Halfway."),
            (25, "Five left."),
        ]
        assert [(c.at_seconds, c.line) for c in cues[1]] == [(15, "Hold.")]

    def test_context_exercises_and_session_voice(self):
        data = {
            "voice": {"session_intro": "Hi.", "session_complete": "Bye."},
            "context_exercises": [
                {"name": "Dead Hangs", "note": "2 min total", "voice": "Hang!"},
                {"name": "Walk", "note": "10k steps"},
            ],
        }
        c = load_cassette_from_dict(data)
        assert c.voice_session_intro == "Hi."
        assert c.voice_session_complete == "Bye."
        assert [(x.name, x.note, x.voice) for x in c.context_exercises] == [
            ("Dead Hangs", "2 min total", "Hang!"),
            ("Walk", "10k steps", None),
        ]


# ---------------------------------------------------------------------------
# Checked-in JSON fixtures load end to end
# ---------------------------------------------------------------------------


def _count_sets(cassette: Cassette) -> int:
    return sum(
        len(ex.sets)
        for phase in cassette.phases
        for group in phase.groups
        for ex in group.exercises
    )


class TestFixtureFiles:
    def test_straight_fixture(self, fixture_cassette):
        c = fixture_cassette("straight")
        assert c.meta["title"] == "Straight Sets Day"
        assert [p.type for p in c.phases] == ["warmup", "main"]
        assert _count_sets(c) == 8  # 2 + 3 + 3
        squat = c.phases[1].groups[0].exercises[0]
        assert [s.reps for s in squat.sets] == [10, 10, 8]  # explicit sets honored
        # group without "rest" inherits meta rest_default
        assert c.phases[1].groups[1].rest == 75
        assert c.context_exercises[0].name == "Dead Hangs"
        assert c.voice_session_intro == "Welcome back. Straight sets today."

    def test_superset_fixture(self, fixture_cassette):
        c = fixture_cassette("superset")
        g = c.phases[0].groups[0]
        assert g.type == "superset"
        assert g.rounds == 3
        assert _count_sets(c) == 6  # 2 exercises x 3 rounds, generated from rounds
        assert [ex.name for ex in g.exercises] == ["Bench Press", "Bent-over Row"]
        assert [s.reps for s in g.exercises[0].sets] == [8, 8, 8]
        assert [s.reps for s in g.exercises[1].sets] == [10, 10, 10]

    def test_timed_holds_fixture(self, fixture_cassette):
        c = fixture_cassette("timed_holds")
        groups = c.phases[0].groups
        assert _count_sets(c) == 4
        assert all(ex.timed for g in groups for ex in g.exercises)
        # per-round cues: plank group has 3 cues round 1, 2 cues round 2
        plank_cues = groups[0].voice_during_set
        assert [len(r) for r in plank_cues] == [3, 2]
        assert plank_cues[0][0].at_seconds == 10
        assert plank_cues[0][0].line == "Squeeze the glutes."


# ---------------------------------------------------------------------------
# text_to_cassette
# ---------------------------------------------------------------------------


class TestTextToCassette:
    def test_one_group_per_exercise_with_rounds_from_sets(self):
        exercises = parse_workout("Squat 3x10 | 60kg\nPlank 2x30s")
        c = text_to_cassette(exercises, rest=45)
        assert c.version == "1.1"
        assert c.meta["rest_default"] == 45
        assert len(c.phases) == 1
        assert c.phases[0].type == "main"
        groups = c.phases[0].groups
        assert len(groups) == 2

        squat = groups[0]
        assert squat.type == "straight"
        assert squat.rounds == 3
        assert squat.rest == 45
        assert squat.exercises[0].name == "Squat"
        assert squat.exercises[0].load == "60kg"
        assert squat.exercises[0].timed is False
        assert [s.reps for s in squat.exercises[0].sets] == [10, 10, 10]

        plank = groups[1]
        assert plank.rounds == 2
        assert plank.exercises[0].timed is True
        assert [s.reps for s in plank.exercises[0].sets] == [30, 30]

    def test_no_voice_lines(self):
        c = text_to_cassette(parse_workout("Squat 3x10"), rest=60)
        g = c.phases[0].groups[0]
        assert c.voice_session_intro is None
        assert c.voice_session_complete is None
        assert g.voice_intro is None
        assert g.voice_round_complete == []
        assert g.voice_group_complete is None

    def test_empty_exercise_list(self):
        c = text_to_cassette([], rest=60)
        assert c.phases[0].groups == []


# ---------------------------------------------------------------------------
# parse_input (JSON vs text detection)
# ---------------------------------------------------------------------------


class TestParseInput:
    def test_json_cassette_detected(self, fixture_cassette):
        raw = json.dumps(fixture_cassette("straight", raw=True))
        cassette, is_json = parse_input(raw, rest=60)
        assert is_json is True
        assert cassette.meta["title"] == "Straight Sets Day"
        assert cassette._source == raw

    def test_json_with_leading_whitespace_detected(self):
        text = '\n\n  {"version": "1.1", "phases": []}'
        cassette, is_json = parse_input(text, rest=60)
        assert is_json is True
        assert cassette.version == "1.1"

    def test_plain_text_parsed_as_legacy_format(self):
        text = "Squat 3x10 | 60kg\nBench 3x8"
        cassette, is_json = parse_input(text, rest=50)
        assert is_json is False
        assert cassette._source == text
        assert cassette.meta["rest_default"] == 50
        names = [g.exercises[0].name for g in cassette.phases[0].groups]
        assert names == ["Squat", "Bench"]

    def test_json_without_phases_key_falls_back_to_text(self):
        text = '{"version": "1.1", "meta": {}}'
        cassette, is_json = parse_input(text, rest=60)
        assert is_json is False
        assert cassette.phases[0].groups == []  # no exercises parsed from it either

    def test_invalid_json_starting_with_brace_falls_back_to_text(self):
        text = "{ this is not json\nSquat 3x10"
        cassette, is_json = parse_input(text, rest=60)
        assert is_json is False
        assert cassette.phases[0].groups[0].exercises[0].name == "Squat"


# ---------------------------------------------------------------------------
# cassette_content_hash
# ---------------------------------------------------------------------------


class TestCassetteContentHash:
    def test_matches_sha256_of_file_bytes(self, tmp_path):
        p = tmp_path / "cassette.json"
        p.write_text('{"version": "1.1", "phases": []}')
        assert cassette_content_hash(str(p)) == hashlib.sha256(p.read_bytes()).hexdigest()

    def test_same_content_same_hash_different_content_different_hash(self, tmp_path):
        a = tmp_path / "a.json"
        b = tmp_path / "b.json"
        c = tmp_path / "c.json"
        a.write_text('{"phases": []}')
        b.write_text('{"phases": []}')
        c.write_text('{"phases": [] }')  # one extra space
        assert cassette_content_hash(str(a)) == cassette_content_hash(str(b))
        assert cassette_content_hash(str(a)) != cassette_content_hash(str(c))
