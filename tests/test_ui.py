"""Tests for exercise_coach.ui: ETA formatting, remaining-time estimation,
progress-bar math, and overview rendering (Rich snapshot substrings)."""

from io import StringIO

from rich.console import Console

from exercise_coach.cassette import load_cassette_from_dict
from exercise_coach.models import Cassette
from exercise_coach.ui import (
    build_overview,
    build_progress_bar,
    estimate_remaining,
    format_eta,
)


def render_to_text(renderable, width: int = 100) -> str:
    console = Console(file=StringIO(), record=True, width=width)
    console.print(renderable)
    return console.export_text()


def complete_round(group, round_idx: int) -> None:
    """Mark every exercise's set for `round_idx` as done at target reps."""
    for ex in group.exercises:
        ex.sets[round_idx].actual_reps = ex.sets[round_idx].reps


# ---------------------------------------------------------------------------
# format_eta
# ---------------------------------------------------------------------------

class TestFormatEta:
    def test_zero_is_done(self):
        assert format_eta(0) == "done"

    def test_negative_is_done(self):
        assert format_eta(-30) == "done"

    def test_seconds_only(self):
        assert format_eta(45) == "45s"

    def test_exact_minute_pads_seconds(self):
        assert format_eta(60) == "1m 00s"

    def test_minutes_and_seconds(self):
        assert format_eta(125) == "2m 05s"


# ---------------------------------------------------------------------------
# estimate_remaining
# ---------------------------------------------------------------------------

class TestEstimateRemaining:
    def test_empty_cassette_is_zero(self):
        cassette = Cassette(version="1.1", meta={})
        assert estimate_remaining(cassette) == 0

    def test_fresh_rep_group(self, straight_cassette):
        # 3 rep sets x 30s each + (3-1) rests of 60s between rounds
        cassette = load_cassette_from_dict(straight_cassette(rounds=3, rest=60))
        assert estimate_remaining(cassette) == 3 * 30 + 2 * 60

    def test_avg_rep_set_override(self, straight_cassette):
        cassette = load_cassette_from_dict(straight_cassette(rounds=2, rest=45))
        assert estimate_remaining(cassette, avg_rep_set=20.0) == 2 * 20 + 1 * 45

    def test_partial_completion_drops_done_sets_and_rests(self, straight_cassette):
        cassette = load_cassette_from_dict(straight_cassette(rounds=3, rest=60))
        group = cassette.phases[0].groups[0]
        complete_round(group, 0)
        # 2 sets left x 30s + (2 rounds left - 1) rest
        assert estimate_remaining(cassette) == 2 * 30 + 1 * 60

    def test_fully_completed_is_zero(self, straight_cassette):
        cassette = load_cassette_from_dict(straight_cassette(rounds=2, rest=60))
        group = cassette.phases[0].groups[0]
        complete_round(group, 0)
        complete_round(group, 1)
        assert estimate_remaining(cassette) == 0

    def test_skipped_group_contributes_nothing(self, straight_cassette):
        cassette = load_cassette_from_dict(straight_cassette(rounds=3, rest=60))
        cassette.phases[0].groups[0].skipped = True
        assert estimate_remaining(cassette) == 0

    def test_timed_sets_use_hold_duration_plus_countdown(self, timed_cassette):
        # 2 timed sets of 30s: each counts 30 + 3, plus (2-1) rest of 45s
        cassette = load_cassette_from_dict(timed_cassette(rounds=2, seconds=30, rest=45))
        assert estimate_remaining(cassette) == 2 * (30 + 3) + 1 * 45

    def test_timed_differs_from_rep_estimate(self, timed_cassette, straight_cassette):
        timed = load_cassette_from_dict(timed_cassette(rounds=1, seconds=60, rest=45))
        rep = load_cassette_from_dict(straight_cassette(rounds=1, rest=45))
        assert estimate_remaining(timed) == 63  # duration-based
        assert estimate_remaining(rep) == 30    # flat avg_rep_set

    def test_superset_counts_all_exercise_sets(self, superset_cassette):
        # 2 exercises x 3 rounds = 6 rep sets, plus 2 rests of 90s
        cassette = load_cassette_from_dict(superset_cassette(rounds=3, rest=90))
        assert estimate_remaining(cassette) == 6 * 30 + 2 * 90


# ---------------------------------------------------------------------------
# build_progress_bar
# ---------------------------------------------------------------------------

class TestBuildProgressBar:
    def test_fresh_cassette_empty_bar(self, straight_cassette):
        cassette = load_cassette_from_dict(straight_cassette(rounds=4, rest=60))
        bar = build_progress_bar(cassette)
        assert "░" * 30 in bar
        assert "█" not in bar
        assert "0/4 sets (0%)" in bar

    def test_partial_fill_math(self, straight_cassette):
        cassette = load_cassette_from_dict(straight_cassette(rounds=4, rest=60))
        complete_round(cassette.phases[0].groups[0], 0)
        bar = build_progress_bar(cassette)
        # 1/4 done -> int(30 * 1/4) = 7 filled, 23 empty
        assert "█" * 7 + "░" * 23 in bar
        assert "1/4 sets (25%)" in bar
        # ETA: 3 sets x 30s + (3 rounds left - 1) x 60s rest = 210s
        assert "ETA: 3m 30s" in bar

    def test_complete_cassette_full_bar(self, straight_cassette):
        cassette = load_cassette_from_dict(straight_cassette(rounds=2, rest=60))
        group = cassette.phases[0].groups[0]
        complete_round(group, 0)
        complete_round(group, 1)
        bar = build_progress_bar(cassette)
        assert "█" * 30 in bar
        assert "░" not in bar
        assert "2/2 sets (100%)" in bar
        assert "ETA: done" in bar

    def test_zero_total_sets_shows_full_bar(self):
        cassette = Cassette(version="1.1", meta={})
        bar = build_progress_bar(cassette)
        assert "█" * 30 in bar
        assert "0/0 sets (100%)" in bar
        assert "ETA: done" in bar


# ---------------------------------------------------------------------------
# build_overview rendering (snapshot substrings, not whole-screen equality)
# ---------------------------------------------------------------------------

class TestBuildOverview:
    def test_superset_fresh_at_playhead(self, fixture_cassette):
        cassette = fixture_cassette("superset")
        text = render_to_text(build_overview(cassette, cur_phase=0, cur_group=0))

        # Title combines meta title and program
        assert "Superset Day — Test Program" in text
        # Phase header
        assert "MAIN" in text
        # Both exercises with superset connectors
        assert "┌ Bench Press" in text
        assert "└ Bent-over Row" in text
        # Current group shows [rounds_completed] progress marker with reps/load
        assert "3[0]×8 | 60kg" in text
        assert "3[0]×10 | 50kg" in text

    def test_superset_progress_marker_advances(self, fixture_cassette):
        cassette = fixture_cassette("superset")
        complete_round(cassette.phases[0].groups[0], 0)
        text = render_to_text(build_overview(cassette, cur_phase=0, cur_group=0))
        assert "3[1]×8" in text
        assert "3[1]×10" in text

    def test_completed_group_keeps_marker_when_not_current(self, fixture_cassette):
        cassette = fixture_cassette("superset")
        group = cassette.phases[0].groups[0]
        for r in range(group.rounds):
            complete_round(group, r)
        # Playhead moved past this group
        text = render_to_text(build_overview(cassette, cur_phase=0, cur_group=1))
        assert "3[3]×8" in text
        assert "3[3]×10" in text

    def test_non_current_unstarted_group_has_no_marker(self, fixture_cassette):
        cassette = fixture_cassette("superset")
        text = render_to_text(build_overview(cassette, cur_phase=0, cur_group=99))
        assert "3×8 | 60kg" in text
        assert "3[0]" not in text

    def test_skipped_group_shows_zero_marker(self, fixture_cassette):
        cassette = fixture_cassette("superset")
        cassette.phases[0].groups[0].skipped = True
        text = render_to_text(build_overview(cassette, cur_phase=0, cur_group=99))
        assert "3[0]×8" in text
        assert "3[0]×10" in text

    def test_straight_fixture_timed_and_context(self, fixture_cassette):
        cassette = fixture_cassette("straight")
        text = render_to_text(build_overview(cassette, cur_phase=1, cur_group=0))

        assert "Straight Sets Day — Test Program" in text
        # Both phase headers
        assert "WARMUP" in text
        assert "MAIN" in text
        # Single-exercise groups get no connectors
        assert "┌" not in text
        assert "└" not in text
        assert "Jumping Jacks" in text
        # Current group (main phase group 0) shows its marker + load
        assert "3[0]×10 | 24kg" in text
        # Non-current groups show plain rounds×reps
        assert "2×20" in text
        assert "3×15" in text
        # Context exercises section
        assert "── Context ──" in text
        assert "Dead Hangs: Accumulate 2 minutes throughout the day." in text

    def test_timed_exercise_reps_render_with_seconds_suffix(self, fixture_cassette):
        cassette = fixture_cassette("timed_holds")
        text = render_to_text(build_overview(cassette, cur_phase=0, cur_group=0))
        # Timed sets show "Ns" (seconds), current group with marker
        assert "[0]×" in text
        assert "s" in text
        # Every timed exercise line must carry the seconds suffix on its reps
        for group in cassette.phases[0].groups:
            for ex in group.exercises:
                assert f"×{ex.sets[0].reps}s" in text
