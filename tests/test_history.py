"""Tests for the JSONL session history and end-of-session summary (rec. §7, PR D).

Covers: the record shape (full and partial), one appended line per session
(normal end, Ctrl-C stop, --only partial), durations measured on the player's
fake clock, corrupt-line resilience when reading, last-session-for-title
lookup, and build_summary_table snapshots with and without a prior session.

All hermetic: history.jsonl lands in tmp_path via conftest's isolated_state,
audio/TTS are recorded (no_audio), and every session is driven headlessly on
a FakeClock with ScriptedKeys.
"""

import json
from io import StringIO

import pytest
from conftest import FakeClock, ScriptedKeys
from rich.console import Console

from exercise_coach import history
from exercise_coach.cassette import load_cassette_from_dict
from exercise_coach.player import play_cassette, play_only_group
from exercise_coach.ui import build_summary_table

# ---------------------------------------------------------------------------
# Helpers (cassette-dict builders mirroring test_player's)
# ---------------------------------------------------------------------------


def ex(name: str, reps: int = 10, timed: bool = False) -> dict:
    return {"name": name, "reps": reps, "timed": timed}


def group(*exercises: dict, rounds: int = 1, rest: int = 30) -> dict:
    return {"type": "straight", "rounds": rounds, "rest": rest, "exercises": list(exercises)}


def cassette(*groups: dict, title: str = "Test") -> dict:
    return {
        "version": "1.1",
        "meta": {"date": "2026-07-12", "title": title, "rest_default": 75},
        "phases": [{"type": "main", "groups": list(groups)}],
    }


def run_session(raw: dict, keys: list[str]) -> float:
    """play_cassette on a fresh parse of `raw`; returns the elapsed fake time."""
    cass = load_cassette_from_dict(raw)
    clock = FakeClock()
    play_cassette(cass, now=clock.now, read_key=ScriptedKeys(keys), sleep=clock.sleep)
    return clock.t - 1_000_000.0


def render(table) -> str:
    console = Console(file=StringIO(), width=80)
    console.print(table)
    return console.file.getvalue()


# ---------------------------------------------------------------------------
# build_record: the record shape
# ---------------------------------------------------------------------------


class TestBuildRecord:
    def make_played_cassette(self):
        """Two groups: Squat done once + failed once; Plank group skipped."""
        cass = load_cassette_from_dict(cassette(
            group(ex("Squat"), rounds=2),
            group(ex("Plank", reps=20, timed=True)),
            title="Shape Day",
        ))
        squat = cass.phases[0].groups[0].exercises[0]
        squat.sets[0].actual_reps = 10
        squat.sets[1].actual_reps = 6
        squat.sets[1].failure = True
        cass.phases[0].groups[1].skipped = True
        return cass

    def test_full_record_shape(self):
        record = history.build_record(
            self.make_played_cassette(), 123.4, {"Squat": 31},
            timestamp="2026-07-12T09:00:00",
        )
        assert record == {
            "schema_version": 1,
            "timestamp": "2026-07-12T09:00:00",
            "title": "Shape Day",
            "program": "",
            "partial": False,
            "matched_name": None,
            "duration_seconds": 123,
            "groups": [
                {"skipped": False, "exercises": [{"name": "Squat", "sets": [
                    {"target": 10, "actual": 10, "failure": False, "timed": False},
                    {"target": 10, "actual": 6, "failure": True, "timed": False},
                ]}]},
                {"skipped": True, "exercises": [{"name": "Plank", "sets": [
                    {"target": 20, "actual": None, "failure": False, "timed": True},
                ]}]},
            ],
            "paces": {"Squat": 31},
        }
        assert history.record_set_counts(record) == (1, 1, 1)  # done/skipped/failed

    def test_partial_record_has_only_the_played_group(self):
        record = history.build_record(
            self.make_played_cassette(), 60, {}, partial=True,
            matched_name="Plank", group_pos=(0, 1), timestamp="2026-07-12T09:00:00",
        )
        assert record["partial"] is True
        assert record["matched_name"] == "Plank"
        assert [e["name"] for g in record["groups"] for e in g["exercises"]] == ["Plank"]

    def test_timestamp_defaults_to_now_iso(self):
        from datetime import datetime
        record = history.build_record(self.make_played_cassette(), 0, {})
        # Parseable ISO, roughly now.
        stamp = datetime.fromisoformat(record["timestamp"])
        assert abs((datetime.now() - stamp).total_seconds()) < 60


# ---------------------------------------------------------------------------
# One appended line per session
# ---------------------------------------------------------------------------


class TestAppendPerSession:
    def test_full_session_appends_one_record_with_player_clock_duration(self, no_audio):
        raw = cassette(group(ex("Squat")), title="History Day")
        # transition, then a 10.0s set (40 empty reads x 0.25s), then done.
        run_session(raw, ["enter"] + [""] * 40 + ["enter"])

        records = history.read_records()
        assert len(records) == 1
        rec = records[0]
        assert rec["title"] == "History Day"
        assert rec["partial"] is False
        assert rec["duration_seconds"] == 10  # the fake clock, not time.time
        assert rec["groups"][0]["exercises"][0]["sets"] == [
            {"target": 10, "actual": 10, "failure": False, "timed": False},
        ]

    def test_two_sessions_append_two_records(self, no_audio):
        raw = cassette(group(ex("Squat")), title="Twice")
        run_session(raw, ["enter", "enter"])
        run_session(raw, ["enter"] + [""] * 20 + ["enter"])

        records = history.read_records()
        assert [r["title"] for r in records] == ["Twice", "Twice"]
        assert [r["duration_seconds"] for r in records] == [0, 5]

    def test_ctrl_c_still_appends_a_record_flagged_partial(self, no_audio):
        cass = load_cassette_from_dict(cassette(group(ex("Squat"), rounds=2), title="Cut Short"))
        clock = FakeClock()
        queue = ["enter"] + [""] * 8 + ["enter"]  # transition + a 2s set 1

        def read_key():
            if queue:
                return queue.pop(0)
            raise KeyboardInterrupt  # Ctrl-C during the rest

        with pytest.raises(KeyboardInterrupt):
            play_cassette(cass, now=clock.now, read_key=read_key, sleep=clock.sleep)

        records = history.read_records()
        assert len(records) == 1
        rec = records[0]
        # partial: an aborted session must never become the "vs last"
        # comparison baseline for the next full run of this title.
        assert rec["partial"] is True
        assert history.last_session_for_title("Cut Short") is None
        assert rec["duration_seconds"] == 2
        sets = rec["groups"][0]["exercises"][0]["sets"]
        assert sets[0]["actual"] == 10
        assert sets[1]["actual"] is None  # cut short: never recorded

    def test_prior_elapsed_backdates_the_session_clock(self, no_audio):
        """A Ctrl-Z/Ctrl-C resume re-enters play_cassette; prior_elapsed
        (the seconds already worked before the re-entry) must be included
        in the record's duration, not silently dropped."""
        cass = load_cassette_from_dict(cassette(group(ex("Squat")), title="Paused Day"))
        clock = FakeClock()
        keys = ScriptedKeys(["enter"] + [""] * 20 + ["enter"])  # 5s of play
        play_cassette(
            cass, now=clock.now, read_key=keys, sleep=clock.sleep,
            prior_elapsed=60.0,
        )
        (rec,) = history.read_records()
        assert rec["duration_seconds"] == 65  # 60s before the pause + 5s now

    def test_prior_elapsed_reaches_the_ctrl_c_record_too(self, no_audio):
        cass = load_cassette_from_dict(cassette(group(ex("Squat"), rounds=2), title="Paused Cut"))
        clock = FakeClock()
        queue = ["enter"] + [""] * 8 + ["enter"]  # transition + a 2s set 1

        def read_key():
            if queue:
                return queue.pop(0)
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            play_cassette(
                cass, now=clock.now, read_key=read_key, sleep=clock.sleep,
                prior_elapsed=30.0,
            )
        (rec,) = history.read_records()
        assert rec["duration_seconds"] == 32

    def test_only_run_appends_a_partial_record(self, no_audio):
        cass = load_cassette_from_dict(cassette(
            group(ex("Squat")), group(ex("Row", reps=12)), title="Only Day",
        ))
        clock = FakeClock()
        keys = ScriptedKeys(["enter"] + [""] * 12 + ["enter"])  # one 3s Row set

        play_only_group(cass, 0, 1, "Row", now=clock.now, read_key=keys, sleep=clock.sleep)

        records = history.read_records()
        assert len(records) == 1
        rec = records[0]
        assert rec["partial"] is True
        assert rec["matched_name"] == "Row"
        assert rec["duration_seconds"] == 3
        # Only the played group, not the whole cassette.
        assert [e["name"] for g in rec["groups"] for e in g["exercises"]] == ["Row"]


# ---------------------------------------------------------------------------
# Reading: corrupt lines, missing file, last-session lookup
# ---------------------------------------------------------------------------


def append_line(text: str) -> None:
    path = history.history_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(text + "\n")


def make_record(title: str, *, timestamp: str, duration: int = 100,
                partial: bool = False) -> dict:
    cass = load_cassette_from_dict(cassette(group(ex("Squat")), title=title))
    cass.phases[0].groups[0].exercises[0].sets[0].actual_reps = 10
    return history.build_record(
        cass, duration, {}, partial=partial,
        matched_name="Squat" if partial else None,
        group_pos=(0, 0) if partial else None, timestamp=timestamp,
    )


class TestReading:
    def test_missing_file_reads_empty(self):
        assert history.read_records() == []
        assert history.last_session_for_title("Anything") is None

    def test_corrupt_and_blank_lines_are_skipped(self):
        good1 = make_record("Good One", timestamp="2026-07-01T08:00:00")
        good2 = make_record("Good Two", timestamp="2026-07-02T08:00:00")
        append_line(json.dumps(good1))
        append_line("{truncated by a crash mid-wri")
        append_line("")
        append_line("not json at all")
        append_line("[1, 2]")  # valid JSON, but not a record object
        append_line(json.dumps(good2))

        records = history.read_records()
        assert [r["title"] for r in records] == ["Good One", "Good Two"]

    def test_last_session_picks_the_right_title_and_most_recent(self):
        history.append_record(make_record("Push Day", timestamp="2026-07-01T08:00:00", duration=100))
        history.append_record(make_record("Pull Day", timestamp="2026-07-02T08:00:00", duration=200))
        history.append_record(make_record("Push Day", timestamp="2026-07-03T08:00:00", duration=300))

        last = history.last_session_for_title("Push Day")
        assert last is not None
        assert (last["timestamp"], last["duration_seconds"]) == ("2026-07-03T08:00:00", 300)
        assert history.last_session_for_title("Leg Day") is None

    def test_last_session_ignores_partial_records(self):
        history.append_record(make_record("Push Day", timestamp="2026-07-01T08:00:00", duration=100))
        history.append_record(
            make_record("Push Day", timestamp="2026-07-02T08:00:00", duration=50, partial=True),
        )
        last = history.last_session_for_title("Push Day")
        assert last["duration_seconds"] == 100  # the full session, not the --only detour


# ---------------------------------------------------------------------------
# build_summary_table snapshots
# ---------------------------------------------------------------------------


class TestSummaryTable:
    def this_session_record(self) -> dict:
        """3 sets done, 1 failed, 1 skipped, 23m 41s."""
        cass = load_cassette_from_dict(cassette(
            group(ex("Squat"), rounds=2), group(ex("Row", reps=12), rounds=2),
            group(ex("Plank", reps=20, timed=True)),
            title="Push Day",
        ))
        for g in cass.phases[0].groups[:2]:
            for s in g.exercises[0].sets:
                s.actual_reps = s.reps
        row = cass.phases[0].groups[1].exercises[0]
        row.sets[1].actual_reps = 8
        row.sets[1].failure = True
        cass.phases[0].groups[2].skipped = True
        return history.build_record(cass, 23 * 60 + 41, {}, timestamp="2026-07-12T09:00:00")

    def test_without_a_prior_session_no_comparison_column(self):
        text = render(build_summary_table(self.this_session_record(), None))
        assert "Session summary — Push Day" in text
        assert "vs last" not in text
        rows = {tuple(line.split()) for line in text.splitlines()}
        for row in (("Total", "time", "23m", "41s"), ("Sets", "done", "3"),
                    ("Sets", "skipped", "1"), ("Sets", "failed", "1")):
            assert row in rows, text

    def test_with_a_prior_session_shows_deltas(self):
        previous = make_record("Push Day", timestamp="2026-07-05T18:30:00", duration=25 * 60)
        # previous: 1 set done, 0 skipped/failed, 25m 0s.
        text = render(build_summary_table(self.this_session_record(), previous))
        assert "vs last (2026-07-05)" in text
        assert "-1m 19s" in text  # 23m41s vs 25m00s
        assert "+2" in text       # 3 done vs 1
        # skipped/failed rows carry no delta — only time and done compare.
        lines = [line for line in text.splitlines() if "Sets skipped" in line]
        assert lines and lines[0].rstrip().endswith("1")

    def test_delta_signs(self):
        record = self.this_session_record()
        slower_less = make_record("Push Day", timestamp="2026-07-05T18:30:00", duration=20 * 60)
        text = render(build_summary_table(record, slower_less))
        assert "+3m 41s" in text  # this session took longer

    def test_full_session_prints_summary_and_compares_to_history(self, no_audio, capsys):
        """End to end: the first run prints a summary with no comparison
        column; a second run of the same title compares against it."""
        raw = cassette(group(ex("Squat")), title="E2E Day")
        run_session(raw, ["enter"] + [""] * 40 + ["enter"])
        out1 = capsys.readouterr().out
        assert "Session summary — E2E Day" in out1
        assert "vs last" not in out1

        run_session(raw, ["enter"] + [""] * 20 + ["enter"])
        out2 = capsys.readouterr().out
        assert "Session summary — E2E Day" in out2
        assert "vs last" in out2
        assert "-5s" in out2   # 5s vs 10s
        assert "+0" in out2    # same sets done

    def test_hand_mangled_previous_record_never_crashes_the_table(self):
        """A line in history.jsonl is user-editable JSON: a shape-mangled but
        parseable previous record must render as zeros, not crash the
        end-of-session summary."""
        mangled = {
            "title": "Push Day",
            "timestamp": 12345,               # not a string
            "duration_seconds": "twenty",     # not a number
            "groups": ["junk", {"exercises": [{"sets": [5, {"actual": 3}]}, "junk"]}],
        }
        assert history.record_set_counts(mangled) == (1, 0, 0)
        assert history.record_duration(mangled) == 0
        text = render(build_summary_table(self.this_session_record(), mangled))
        assert "vs last" in text
        assert "+23m 41s" in text  # delta against a 0s previous

    def test_resumed_completion_compares_to_the_real_previous_session(self, no_audio, capsys):
        """Ctrl-C then resume-to-completion: the summary must compare against
        the last *real* session with this title, not the abort stub."""
        history.append_record(make_record(
            "Abort Day", timestamp="2026-07-01T08:00:00", duration=500,
        ))
        cass = load_cassette_from_dict(cassette(group(ex("Squat"), rounds=2), title="Abort Day"))
        clock = FakeClock()
        queue = ["enter"] + [""] * 8 + ["enter"]  # set 1, then Ctrl-C in the rest

        def read_key():
            if queue:
                return queue.pop(0)
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            play_cassette(cass, now=clock.now, read_key=read_key, sleep=clock.sleep)
        capsys.readouterr()

        # Resume the same progressed cassette (what cli's resume flow does).
        clock2 = FakeClock()
        keys = ScriptedKeys(["enter"] + [""] * 8 + ["enter", "enter"])
        play_cassette(cass, now=clock2.now, read_key=keys, sleep=clock2.sleep)
        out = capsys.readouterr().out
        assert "vs last (2026-07-01)" in out  # the real session, not today's stub

    @pytest.mark.parametrize("mangled", [
        {"title": "Push Day", "groups": None},
        {"title": "Push Day", "groups": 5},
        {"title": "Push Day", "groups": [{"exercises": None}]},
        {"title": "Push Day", "groups": [{"exercises": 7}]},
        {"title": "Push Day", "groups": [{"exercises": [{"sets": None}]}]},
        {"title": "Push Day", "groups": [{"exercises": [{"sets": 3}]}]},
    ])
    def test_non_list_containers_never_crash_the_table(self, mangled):
        """Wrong-typed *containers* (null/number where a list belongs — a
        natural hand-edit, since the schema uses null for other fields) must
        count as zeros, not TypeError at 'Workout complete!'."""
        assert history.record_set_counts(mangled) == (0, 0, 0)
        text = render(build_summary_table(self.this_session_record(), mangled))
        assert "vs last" in text

    def test_ctrl_c_and_only_runs_print_no_summary(self, no_audio, capsys):
        cass = load_cassette_from_dict(cassette(group(ex("Squat")), title="No Summary"))
        clock = FakeClock()
        keys = ScriptedKeys(["enter"] + [""] * 8 + ["enter"])
        play_only_group(cass, 0, 0, "Squat", now=clock.now, read_key=keys, sleep=clock.sleep)
        assert "Session summary" not in capsys.readouterr().out

        cass2 = load_cassette_from_dict(cassette(group(ex("Squat"), rounds=2), title="No Summary"))
        clock2 = FakeClock()

        def read_key():
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            play_cassette(cass2, now=clock2.now, read_key=read_key, sleep=clock2.sleep)
        assert "Session summary" not in capsys.readouterr().out
