"""Acceptance tests for cassette spec v1.2: `tempo` and `per_side` display
fields, version acceptance, and forward compatibility.

Mirrors the six acceptance tests in the v1.2 delta spec:
1. v1.1 cassette renders byte-for-byte as before (incl. log format).
2. `tempo` shows on every set's panel display, never in the log.
3. `per_side` renders the rep target as N/side; log format unchanged.
4. Both fields together: "12/side · 3s eccentric".
5. Unknown future version -> one-line stderr warning, best-effort parse.
6. Unknown fields anywhere -> silently ignored.
"""

from io import StringIO

from rich.console import Console

from exercise_coach.cassette import load_cassette_from_dict
from exercise_coach.state import format_exercise_log, render_log
from exercise_coach.ui import (
    build_active_panel_straight,
    build_active_panel_superset,
    build_overview,
)


def render_to_text(renderable, width: int = 100) -> str:
    console = Console(file=StringIO(), record=True, width=width)
    console.print(renderable)
    return console.export_text()


def complete_all(cassette) -> None:
    """Mark every set done at target so the log renders the clean N×M form."""
    for phase in cassette.phases:
        for group in phase.groups:
            for ex in group.exercises:
                for s in ex.sets:
                    s.actual_reps = s.reps


def v11_cassette(exercise: dict, *, rounds: int = 4, group_type: str = "straight") -> dict:
    return {
        "version": "1.1",
        "meta": {"date": "2026-07-12", "title": "Test", "rest_default": 75},
        "phases": [{"type": "main", "groups": [{
            "type": group_type, "rounds": rounds, "rest": 60,
            "exercises": [exercise] if group_type == "straight" else exercise,
        }]}],
    }


# ---------------------------------------------------------------------------
# 1. v1.1 cassettes (no new fields) render exactly as before
# ---------------------------------------------------------------------------

class TestV11Unchanged:
    def test_straight_panel_rep_label_unchanged(self):
        c = load_cassette_from_dict(v11_cassette(
            {"name": "Goblet Squat", "load": "24kg", "reps": 10}, rounds=3))
        ex = c.phases[0].groups[0].exercises[0]
        text = render_to_text(build_active_panel_straight(ex, 0, 3))
        assert "Round 1 of 3  •  10 reps" in text
        assert "/side" not in text
        assert "·" not in text

    def test_timed_panel_rep_label_unchanged(self):
        c = load_cassette_from_dict(v11_cassette(
            {"name": "Plank", "load": "BW", "timed": True, "reps": 30}, rounds=2))
        ex = c.phases[0].groups[0].exercises[0]
        text = render_to_text(build_active_panel_straight(ex, 1, 2))
        assert "Round 2 of 2  •  30s hold" in text

    def test_superset_panel_rep_labels_unchanged(self):
        c = load_cassette_from_dict(v11_cassette(
            [{"name": "Bench Press", "load": "60kg", "reps": 8},
             {"name": "Bent-over Row", "load": "50kg", "reps": 10}],
            rounds=3, group_type="superset"))
        text = render_to_text(build_active_panel_superset(c.phases[0].groups[0], 0, 0))
        assert "► Bench Press  •  8 reps  •  60kg" in text
        assert "  Bent-over Row  •  10 reps  •  50kg" in text

    def test_overview_row_unchanged(self, fixture_cassette):
        c = fixture_cassette("straight")
        text = render_to_text(build_overview(c, cur_phase=0, cur_group=99))
        assert "3×10 | 24kg" in text
        assert "2×20" in text

    def test_log_format_unchanged(self, fixture_cassette):
        c = fixture_cassette("straight")
        complete_all(c)
        log = render_log(c)
        assert f"{'Goblet Squat':<25} 3×10 | 24kg" in log
        assert f"{'Jumping Jacks':<25} 2×20" in log

    def test_log_format_regression_on_per_side_exercise(self):
        """The log line of a per_side exercise is byte-identical to the same
        exercise without per_side — `4×10` still means 10 per side."""
        plain = load_cassette_from_dict(v11_cassette(
            {"name": "One-Arm DB Rows", "load": "55 lbs", "reps": 10}))
        data = v11_cassette(
            {"name": "One-Arm DB Rows", "load": "55 lbs", "reps": 10, "per_side": True})
        data["version"] = "1.2"
        sided = load_cassette_from_dict(data)
        for c in (plain, sided):
            complete_all(c)
        plain_group = plain.phases[0].groups[0]
        sided_group = sided.phases[0].groups[0]
        plain_line = format_exercise_log(plain_group.exercises[0], plain_group)
        sided_line = format_exercise_log(sided_group.exercises[0], sided_group)
        assert sided_line == plain_line
        assert sided_line == f"{'One-Arm DB Rows':<25} 4×10 | 55 lbs"


# ---------------------------------------------------------------------------
# 2. tempo only: shown on every set's display, never in the log
# ---------------------------------------------------------------------------

class TestTempo:
    def test_tempo_on_every_set_panel(self, fixture_cassette):
        c = fixture_cassette("v12_fields")
        rdl_group = c.phases[0].groups[1]
        ex = rdl_group.exercises[0]
        assert ex.tempo == "3s eccentric"
        for round_idx in range(rdl_group.rounds):
            text = render_to_text(build_active_panel_straight(ex, round_idx, rdl_group.rounds))
            assert "13 reps · 3s eccentric" in text

    def test_tempo_absent_on_other_exercises(self, fixture_cassette):
        c = fixture_cassette("v12_fields")
        bench = c.phases[0].groups[0].exercises[0]
        assert bench.tempo is None
        text = render_to_text(build_active_panel_superset(c.phases[0].groups[0], 0, 0))
        assert "eccentric" not in text

    def test_tempo_in_overview(self, fixture_cassette):
        c = fixture_cassette("v12_fields")
        text = render_to_text(build_overview(c, cur_phase=0, cur_group=0), width=120)
        assert "4×13 · 3s eccentric | 55 lbs" in text

    def test_tempo_never_in_log(self, fixture_cassette):
        c = fixture_cassette("v12_fields")
        complete_all(c)
        log = render_log(c)
        assert "eccentric" not in log
        assert "·" not in log
        assert f"{'DB RDLs':<25} 4×13 | 55 lbs" in log


# ---------------------------------------------------------------------------
# 3. per_side: display shows N/side, log stays N×M; timed keeps one timer
# ---------------------------------------------------------------------------

class TestPerSide:
    def test_per_side_in_superset_panel(self, fixture_cassette):
        c = fixture_cassette("v12_fields")
        group = c.phases[0].groups[0]
        assert group.exercises[1].per_side is True
        text = render_to_text(build_active_panel_superset(group, 0, 1))
        assert "► One-Arm DB Rows  •  10/side  •  55 lbs" in text
        assert "DB Bench Press  •  10 reps  •  55 lbs" in text  # untouched

    def test_per_side_in_straight_panel(self):
        data = v11_cassette(
            {"name": "Bulgarian Split Squat", "load": "35 lbs", "reps": 12, "per_side": True})
        data["version"] = "1.2"
        c = load_cassette_from_dict(data)
        text = render_to_text(build_active_panel_straight(c.phases[0].groups[0].exercises[0], 0, 4))
        assert "12/side" in text
        assert "12 reps" not in text

    def test_per_side_in_overview(self, fixture_cassette):
        c = fixture_cassette("v12_fields")
        text = render_to_text(build_overview(c, cur_phase=0, cur_group=99), width=120)
        assert "4×10/side | 55 lbs" in text
        # The current-group marker form keeps the transform too.
        current = render_to_text(build_overview(c, cur_phase=0, cur_group=0), width=120)
        assert "4[0]×10/side | 55 lbs" in current

    def test_per_side_log_shows_plain_reps(self, fixture_cassette):
        c = fixture_cassette("v12_fields")
        complete_all(c)
        log = render_log(c)
        assert "/side" not in log
        assert f"{'One-Arm DB Rows':<25} 4×10 | 55 lbs" in log

    def test_timed_per_side_renders_seconds_per_side(self):
        data = v11_cassette(
            {"name": "Copenhagen Plank", "load": "BW", "timed": True,
             "reps": 30, "per_side": True}, rounds=2)
        data["version"] = "1.2"
        c = load_cassette_from_dict(data)
        text = render_to_text(build_active_panel_straight(c.phases[0].groups[0].exercises[0], 0, 2))
        assert "30s/side" in text

    def test_timed_per_side_keeps_single_timer(self, make_player):
        """per_side on a timed hold does not split the timer: one countdown,
        one hold, one recorded duration per set."""
        data = v11_cassette(
            {"name": "Copenhagen Plank", "load": "BW", "timed": True,
             "reps": 3, "per_side": True}, rounds=1)
        data["version"] = "1.2"
        h = make_player(data, keys=["enter"])  # transition only; timer elapses
        h.run()
        assert h.player.is_complete()
        hold = h.cassette.phases[0].groups[0].exercises[0]
        assert [s.actual_reps for s in hold.sets] == [3]  # one hold, full duration


# ---------------------------------------------------------------------------
# 4. Both fields together
# ---------------------------------------------------------------------------

class TestBothFields:
    def make(self) -> dict:
        data = v11_cassette(
            {"name": "Bulgarian Split Squat", "load": "35 lbs", "reps": 12,
             "per_side": True, "tempo": "3s eccentric"})
        data["version"] = "1.2"
        return data

    def test_ordering_in_straight_panel(self):
        c = load_cassette_from_dict(self.make())
        text = render_to_text(build_active_panel_straight(c.phases[0].groups[0].exercises[0], 0, 4))
        assert "12/side · 3s eccentric" in text

    def test_ordering_in_superset_panel(self):
        data = self.make()
        data["phases"][0]["groups"][0]["type"] = "superset"
        data["phases"][0]["groups"][0]["exercises"].append(
            {"name": "DB Bench Press", "load": "55 lbs", "reps": 10})
        c = load_cassette_from_dict(data)
        text = render_to_text(build_active_panel_superset(c.phases[0].groups[0], 0, 0))
        assert "12/side · 3s eccentric" in text

    def test_ordering_in_overview(self):
        c = load_cassette_from_dict(self.make())
        text = render_to_text(build_overview(c, cur_phase=0, cur_group=99), width=120)
        assert "4×12/side · 3s eccentric | 35 lbs" in text

    def test_log_still_plain(self):
        c = load_cassette_from_dict(self.make())
        complete_all(c)
        log = render_log(c)
        assert f"{'Bulgarian Split Squat':<25} 4×12 | 35 lbs" in log
        assert "/side" not in log
        assert "eccentric" not in log


# ---------------------------------------------------------------------------
# 5. Version acceptance
# ---------------------------------------------------------------------------

class TestVersionAcceptance:
    def test_v11_and_v12_accepted_silently(self, capsys):
        for version in ("1.1", "1.2"):
            data = v11_cassette({"name": "Squat", "reps": 10})
            data["version"] = version
            c = load_cassette_from_dict(data)
            assert c.version == version
        assert capsys.readouterr().err == ""

    def test_future_version_warns_once_and_parses(self, capsys):
        data = v11_cassette({"name": "Squat", "load": "24kg", "reps": 10}, rounds=3)
        data["version"] = "1.3"
        c = load_cassette_from_dict(data)  # must not raise
        err = capsys.readouterr().err
        assert err.count("\n") == 1  # one-line warning
        assert "1.3" in err
        assert c.version == "1.3"
        # Best-effort playback: the cassette parsed fully.
        ex = c.phases[0].groups[0].exercises[0]
        assert ex.name == "Squat"
        assert [s.reps for s in ex.sets] == [10, 10, 10]


# ---------------------------------------------------------------------------
# 6. Unknown fields are silently ignored
# ---------------------------------------------------------------------------

class TestUnknownFieldsIgnored:
    def test_unknown_fields_everywhere(self, capsys):
        data = v11_cassette(
            {"name": "Squat", "load": "24kg", "reps": 10, "foo": 1}, rounds=2)
        data["version"] = "1.2"
        data["bar"] = {"nested": True}
        data["meta"]["baz"] = "x"
        data["phases"][0]["qux"] = [1, 2, 3]
        data["phases"][0]["groups"][0]["quux"] = "y"
        data["phases"][0]["groups"][0]["exercises"][0]["sets"] = [
            {"reps": 10, "rpe": 8}, {"reps": 10, "rpe": 9},
        ]
        c = load_cassette_from_dict(data)  # must not raise
        assert capsys.readouterr().err == ""  # silent
        ex = c.phases[0].groups[0].exercises[0]
        assert ex.name == "Squat"
        assert [s.reps for s in ex.sets] == [10, 10]
        text = render_to_text(build_active_panel_straight(ex, 0, 2))
        assert "10 reps" in text
        assert "foo" not in text
