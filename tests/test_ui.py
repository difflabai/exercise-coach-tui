"""Tests for exercise_coach.ui: ETA formatting, remaining-time estimation,
pace persistence, progress-bar math, and overview rendering (Rich snapshot
substrings)."""

from io import StringIO

from rich.console import Console

from exercise_coach import settings as user_settings
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


class TestEstimateRemainingLiveRestAndPaces:
    """The §6 ETA fixes: the in-progress rest is charged via `rest_remaining`
    (the cassette alone can't see it) and per-set charges prefer the learned
    per-exercise pace over the session average."""

    def test_live_rest_is_charged_on_top_of_structural_rests(self, straight_cassette):
        cassette = load_cassette_from_dict(straight_cassette(rounds=3, rest=60))
        complete_round(cassette.phases[0].groups[0], 0)
        # Mid-rest after round 1, 45s on the clock: 2 sets + the one rest
        # still between rounds 2 and 3 + the live 45s.
        assert estimate_remaining(cassette, 30.0, rest_remaining=45) == 2 * 30 + 60 + 45

    def test_round_boundary_has_no_cliff(self, straight_cassette):
        """The instant a round completes, the estimate with the full rest
        passed live must equal the pre-completion estimate minus only the
        set that was just performed — not minus a whole rest period."""
        cassette = load_cassette_from_dict(straight_cassette(rounds=3, rest=60))
        before = estimate_remaining(cassette)  # 3*30 + 2*60 = 210
        complete_round(cassette.phases[0].groups[0], 0)
        at_rest_start = estimate_remaining(cassette, rest_remaining=60)
        assert at_rest_start == before - 30

    def test_overtime_rest_clamps_to_zero(self, straight_cassette):
        cassette = load_cassette_from_dict(straight_cassette(rounds=2, rest=60))
        complete_round(cassette.phases[0].groups[0], 0)
        base = estimate_remaining(cassette)
        assert estimate_remaining(cassette, rest_remaining=-12.5) == base

    def test_learned_pace_beats_session_average(self, straight_cassette):
        cassette = load_cassette_from_dict(
            straight_cassette(name="Goblet Squat", rounds=2, rest=45)
        )
        est = estimate_remaining(cassette, 25.0, paces={"Goblet Squat": 18})
        assert est == 2 * 18 + 45

    def test_unknown_exercise_falls_back_to_session_average(self, straight_cassette):
        cassette = load_cassette_from_dict(straight_cassette(rounds=2, rest=45))
        est = estimate_remaining(cassette, 25.0, paces={"Some Other Lift": 18})
        assert est == 2 * 25 + 45

    def test_timed_holds_ignore_paces(self, timed_cassette):
        cassette = load_cassette_from_dict(timed_cassette(rounds=1, seconds=30, rest=45))
        assert estimate_remaining(cassette, paces={"Plank": 5}) == 30 + 3

    def test_pending_rest_charges_one_extra_full_rest(self, straight_cassette):
        """A rest interrupted by 'b'/'j' restarts in full on re-entry, so a
        mid-round group in `pending_rests` costs one extra rest period —
        without it the ETA under-states by the whole rest while the user is
        on any other group's screens."""
        cassette = load_cassette_from_dict(straight_cassette(rounds=2, rest=60))
        complete_round(cassette.phases[0].groups[0], 0)
        base = estimate_remaining(cassette)  # 1 set left, 0 structural rests
        assert base == 30
        assert estimate_remaining(cassette, pending_rests={(0, 0)}) == base + 60

    def test_pending_rest_for_fresh_or_done_group_is_ignored(self, straight_cassette):
        """Mirrors play_group's replay guard (0 < rc < rounds): a stale
        pending entry for a fresh (redone) or completed group costs nothing."""
        fresh = load_cassette_from_dict(straight_cassette(rounds=2, rest=60))
        assert (
            estimate_remaining(fresh, pending_rests={(0, 0)})
            == estimate_remaining(fresh)
        )
        done = load_cassette_from_dict(straight_cassette(rounds=2, rest=60))
        complete_round(done.phases[0].groups[0], 0)
        complete_round(done.phases[0].groups[0], 1)
        assert estimate_remaining(done, pending_rests={(0, 0)}) == 0


class TestPaceSettings:
    """settings.py pace persistence: merge-on-save, sanitizing, and the cap."""

    def test_round_trip(self, isolated_state):
        user_settings.update_paces({"Goblet Squat": 22, "Bench Press": 31})
        user_settings.paces = {}
        user_settings.load_paces()
        assert user_settings.paces == {"Goblet Squat": 22, "Bench Press": 31}

    def test_update_merges_and_keeps_other_settings_keys(self, isolated_state):
        user_settings.save_data({"volume": 0.5, "buddy": False})
        user_settings.update_paces({"Row": 28})
        user_settings.update_paces({"Row": 25, "Curl": 19})

        data = user_settings.load_data()
        assert data["paces"] == {"Row": 25, "Curl": 19}
        assert data["volume"] == 0.5
        assert data["buddy"] is False

    def test_invalid_and_nonpositive_entries_are_dropped(self, isolated_state):
        user_settings.save_data({"paces": {"Good": 20, "Bad": "fast", "Zero": 0, "Flag": True}})
        user_settings.load_paces()
        assert user_settings.paces == {"Good": 20}
        user_settings.update_paces({"Noise": 0})
        assert "Noise" not in user_settings.paces

    def test_infinity_and_nan_in_file_are_dropped_not_crashes(self, isolated_state):
        """TOML accepts inf/nan/1e999 floats — a hand-edited file must never
        crash startup (load_paces) or the session-end update_paces merge
        with an OverflowError."""
        path = user_settings.config_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "[paces]\nRDL = inf\nCurl = nan\nBig = 1e999\nGood = 20\n"
        )
        user_settings.load_paces()
        assert user_settings.paces == {"Good": 20}
        user_settings.update_paces({"Squat": 25})  # merge path reads the file too
        assert user_settings.paces == {"Good": 20, "Squat": 25}

    def test_subsecond_float_is_dropped_not_stored_as_zero(self, isolated_state):
        """0.5 passed the old raw-value filter (0.5 > 0) but int() stored a
        0 pace, making every remaining set of that exercise free in the ETA."""
        user_settings.save_data({"paces": {"Squat": 0.5, "Row": 20.9}})
        user_settings.load_paces()
        assert user_settings.paces == {"Row": 20}  # truncated, but positive

    def test_cap_evicts_longest_untouched_first(self, isolated_state):
        user_settings.update_paces(
            {f"Exercise {i}": 20 for i in range(user_settings.MAX_PACES)}
        )
        user_settings.update_paces({"Exercise 0": 21, "Newcomer": 30})

        assert len(user_settings.paces) == user_settings.MAX_PACES
        assert user_settings.paces["Newcomer"] == 30
        assert user_settings.paces["Exercise 0"] == 21  # re-inserted, survives
        assert "Exercise 1" not in user_settings.paces  # oldest untouched evicted


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

    def test_learned_paces_reach_the_rendered_eta(self, straight_cassette):
        """The end-to-end wiring finding: build_progress_bar must feed the
        loaded user_settings.paces into estimate_remaining — with the global
        set, the rendered ETA changes; disconnecting it (paces=None) would
        fall back to 2*30+60 = "2m 00s"."""
        cassette = load_cassette_from_dict(
            straight_cassette(name="Goblet Squat", rounds=2, rest=60)
        )
        assert "ETA: 2m 00s" in build_progress_bar(cassette)  # default pace
        user_settings.paces = {"Goblet Squat": 90}
        assert "ETA: 4m 00s" in build_progress_bar(cassette)  # 2*90 + 60

    def test_pending_rests_reach_the_rendered_eta(self, straight_cassette):
        """Same wiring guarantee for ui.pending_rests (bound by the Player):
        a mid-round pending rest must show up in every screen's ETA."""
        from exercise_coach import ui

        cassette = load_cassette_from_dict(straight_cassette(rounds=2, rest=60))
        complete_round(cassette.phases[0].groups[0], 0)
        assert "ETA: 30s" in build_progress_bar(cassette)  # one 30s set left
        ui.pending_rests = {(0, 0)}
        assert "ETA: 1m 30s" in build_progress_bar(cassette)  # + the owed 60s


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
