"""Player state-machine tests: headless, scripted keys, fake clock.

Every test drives `Player` through `make_player` (see conftest): screens get
an injected read_key/now/sleep, audio is recorded instead of played, and
state files land in tmp_path.

Key-script arithmetic: a group needs 1 "enter" for its transition screen,
one per rep set, and one to end each rest (rests happen between rounds, so
rounds - 1 of them).
"""

import json

from conftest import FakeClock, ScriptedKeys

from exercise_coach import state as state_mod
from exercise_coach.cassette import count_sets, load_cassette_from_dict, rounds_completed
from exercise_coach.events import Event
from exercise_coach.player import play_cassette
from exercise_coach.state import apply_state, load_state_data

# ---------------------------------------------------------------------------
# Local cassette-dict builders (multi-group layouts the conftest factories
# don't cover)
# ---------------------------------------------------------------------------

def ex(name: str, reps: int = 10, load: str = "") -> dict:
    return {"name": name, "reps": reps, "load": load}


def group(*exercises: dict, type: str = "straight", rounds: int = 1, rest: int = 30) -> dict:
    return {"type": type, "rounds": rounds, "rest": rest, "exercises": list(exercises)}


def cassette(*groups: dict, title: str = "Test") -> dict:
    return {
        "version": "1.1",
        "meta": {"date": "2026-07-12", "title": title, "rest_default": 75},
        "phases": [{"type": "main", "groups": list(groups)}],
    }


def keys_to_finish(rounds: int, n_ex: int = 1) -> list[str]:
    """Enter for transition + one per set + one per between-round rest."""
    return ["enter"] * (1 + rounds * n_ex + (rounds - 1))


def complete_group(g) -> None:
    """Mark every set in a (parsed) Group as done at its target."""
    for e in g.exercises:
        for s in e.sets:
            s.actual_reps = s.reps


def all_sets(g) -> list:
    return [s for e in g.exercises for s in e.sets]


# ---------------------------------------------------------------------------
# Linear advance
# ---------------------------------------------------------------------------

def test_linear_advance_multiphase_straight(make_player, fixture_cassette, no_audio):
    """straight.json: warmup (2 rounds) + two main groups (3 rounds each)."""
    keys = keys_to_finish(2) + keys_to_finish(3) + keys_to_finish(3)
    h = make_player(fixture_cassette("straight", raw=True), keys=keys)

    h.run()

    assert h.player.is_complete()
    assert count_sets(h.cassette) == (8, 8)
    # Explicit sets list 10/10/8 is honored, not the group default.
    squat = h.cassette.phases[1].groups[0].exercises[0]
    assert [s.actual_reps for s in squat.sets] == [10, 10, 8]
    assert not any(s.failure for s in squat.sets)
    # Both phase intros were spoken, once each, in order.
    spoken = no_audio.spoken
    assert spoken.index("Warmup first.") < spoken.index("Main work. Two movements.")
    assert spoken.count("Warmup first.") == 1
    assert "Squats done." in spoken


def test_linear_advance_superset_and_circuit(make_player, no_audio):
    cass = cassette(
        group(ex("Bench", 8), ex("Row", 10), type="superset", rounds=2),
        group(ex("KB Swing", 15), ex("Push-up", 12), ex("Plank Row", 8),
              type="circuit", rounds=2),
    )
    keys = keys_to_finish(2, n_ex=2) + keys_to_finish(2, n_ex=3)
    h = make_player(cass, keys=keys)

    h.run()

    assert h.player.is_complete()
    assert count_sets(h.cassette) == (10, 10)
    for g in h.cassette.phases[0].groups:
        for e in g.exercises:
            assert [s.actual_reps for s in e.sets] == [s.reps for s in e.sets]
    # One set-chime per set plus one per round: (2+1)*2 + (3+1)*2 = 14.
    assert no_audio.sounds.count(b"set_complete") == 14
    assert no_audio.sounds.count(b"group_complete") == 2


def test_timed_hold_completes_and_fires_cues(make_player, timed_cassette, no_audio):
    cass = timed_cassette(rounds=1, seconds=3, cues=[[(1, "Halfway there.")]])
    h = make_player(cass, keys=["enter"])  # transition only; the timer elapses

    h.run()

    assert h.player.is_complete()
    hold = h.cassette.phases[0].groups[0].exercises[0]
    assert hold.sets[0].actual_reps == 3  # timed holds record the duration
    for line in ("Get in position", "Go", "Halfway there.", "Done"):
        assert line in no_audio.spoken


# ---------------------------------------------------------------------------
# Skip
# ---------------------------------------------------------------------------

def test_skip_at_transition_leaves_later_groups_untouched(make_player):
    cass = cassette(group(ex("A")), group(ex("B")), group(ex("C")))
    keys = keys_to_finish(1) + ["s"] + keys_to_finish(1)
    h = make_player(cass, keys=keys)

    h.run()

    g1, g2, g3 = h.cassette.phases[0].groups
    assert g2.skipped
    assert all(s.actual_reps is None for s in all_sets(g2))  # skip records nothing
    assert all(s.actual_reps is not None for s in all_sets(g1))
    assert all(s.actual_reps is not None for s in all_sets(g3))
    assert h.player.is_complete()  # skipped counts as done


def test_skip_mid_set_preserves_earlier_progress(make_player, no_audio):
    cass = cassette(group(ex("Squat", 10), rounds=2), group(ex("Row", 5)))
    # G1: transition, set 1, rest, then 's' during set 2. G2: full.
    keys = ["enter", "enter", "enter", "s"] + keys_to_finish(1)
    h = make_player(cass, keys=keys)

    h.run()

    g1, g2 = h.cassette.phases[0].groups
    assert g1.skipped
    assert g1.exercises[0].sets[0].actual_reps == 10  # round 1 kept
    assert g1.exercises[0].sets[1].actual_reps is None
    assert "Skipping Squat" in no_audio.spoken
    assert all(s.actual_reps == 5 for s in all_sets(g2))
    assert h.player.is_complete()


# ---------------------------------------------------------------------------
# Back navigation
# ---------------------------------------------------------------------------

def test_back_is_non_destructive(make_player):
    cass = cassette(group(ex("Squat", 10)), group(ex("Row", 5)))
    h = make_player(cass, keys=["enter", "enter"])

    assert h.play_group() is Event.DONE  # complete group 1
    g1 = h.cassette.phases[0].groups[0]
    assert g1.exercises[0].sets[0].actual_reps == 10

    assert h.player.advance_group()
    h.keys.push("b")
    assert h.play_group() is Event.BACK  # 'b' on group 2's transition
    assert h.player.previous_group()
    assert (h.player.phase_idx, h.player.group_idx) == (0, 0)

    # The move back cleared nothing.
    assert g1.exercises[0].sets[0].actual_reps == 10

    # Landing on the finished group offers redo; Enter just moves on,
    # still without touching progress.
    h.keys.push("enter")
    assert h.play_group(via_back=True) is Event.DONE
    assert g1.exercises[0].sets[0].actual_reps == 10
    assert not g1.exercises[0].sets[0].failure


def test_back_then_forward_resumes_at_first_incomplete_set(make_player, no_audio):
    cass = cassette(
        group(ex("Squat", 10)),
        group(ex("Bench", 8), ex("Row", 12), type="superset"),
    )
    # G1 done; G2: transition, Bench set, then 'b' during Row; back onto the
    # finished G1 (enter = continue); forward re-enters G2, which must skip
    # the already-done Bench set and ask only for Row.
    keys = ["enter", "enter",        # G1
            "enter", "enter", "b",   # G2 first visit
            "enter",                 # G1 completed-transition: continue
            "enter", "enter"]        # G2 revisit: transition + Row only
    h = make_player(cass, keys=keys)

    h.run()

    assert h.player.is_complete()
    bench, row = h.cassette.phases[0].groups[1].exercises
    assert bench.sets[0].actual_reps == 8
    assert row.sets[0].actual_reps == 12
    # Bench was performed exactly once: G1 gives 2 set-chimes (set + round),
    # Bench 1, then Row + round-complete = 2. A replayed Bench would add one.
    assert no_audio.sounds.count(b"set_complete") == 5


def test_pending_rest_restarts_after_back_during_rest(make_player, no_audio):
    cass = cassette(group(ex("Squat", 10)), group(ex("Row", 5), rounds=2, rest=2))
    keys = (
        ["enter", "enter"]           # G1
        + ["enter", "enter", "b"]    # G2: transition, set 1, 'b' during rest
        + ["enter"]                  # G1 completed-transition: continue
        + [""] * 12                  # restarted rest runs past 0 (fake clock)
        + ["enter", "enter"]         # end rest, set 2
    )
    h = make_player(cass, keys=keys)

    h.run()

    assert h.player.is_complete()
    # The rest-done chime can only fire if a rest timer counted down past
    # zero — the first rest was interrupted immediately, so this proves the
    # rest was restarted on re-entry rather than dropping into set 2.
    assert no_audio.sounds.count(b"rest_done") == 1
    row = h.cassette.phases[0].groups[1].exercises[0]
    assert [s.actual_reps for s in row.sets] == [5, 5]


# ---------------------------------------------------------------------------
# Redo
# ---------------------------------------------------------------------------

def test_redo_clears_exactly_one_group(make_player):
    cass = cassette(group(ex("Squat", 10)), group(ex("Row", 5)))
    h = make_player(cass)

    # G1: fail at 7 reps.
    h.keys.push("enter", "f", "7", "enter")
    assert h.play_group() is Event.DONE
    squat = h.cassette.phases[0].groups[0].exercises[0]
    assert squat.sets[0].actual_reps == 7
    assert squat.sets[0].failure

    # G2: complete normally.
    assert h.player.advance_group()
    h.keys.push("enter", "enter")
    assert h.play_group() is Event.DONE

    # Back onto G1 and redo it clean.
    assert h.player.previous_group()
    h.keys.push("r", "enter")
    assert h.play_group(via_back=True) is Event.DONE

    assert squat.sets[0].actual_reps == 10  # replayed at target
    assert not squat.sets[0].failure        # failure flag cleared by redo
    row = h.cassette.phases[0].groups[1].exercises[0]
    assert row.sets[0].actual_reps == 5     # the other group was untouched


# ---------------------------------------------------------------------------
# Advance-to-next-incomplete
# ---------------------------------------------------------------------------

def test_advance_group_skips_completed_groups(make_player):
    cass = cassette(group(ex("A")), group(ex("B")), group(ex("C")))
    h = make_player(cass, keys=["enter", "enter"])
    complete_group(h.cassette.phases[0].groups[1])  # G2 already done

    assert h.play_group() is Event.DONE  # G1
    assert h.player.advance_group()
    assert (h.player.phase_idx, h.player.group_idx) == (0, 2)  # skipped G2

    # And it wraps: from the last group, the earlier incomplete one is found.
    h2 = make_player(cass)
    complete_group(h2.cassette.phases[0].groups[1])
    complete_group(h2.cassette.phases[0].groups[2])
    h2.player.jump_to(0, 2)
    assert h2.player.advance_group()
    assert (h2.player.phase_idx, h2.player.group_idx) == (0, 0)


# ---------------------------------------------------------------------------
# Resume from saved state
# ---------------------------------------------------------------------------

def test_resume_from_mid_workout_state(make_player):
    cass = cassette(group(ex("Squat", 10), rounds=2), group(ex("Row", 5)))

    # Session 1: finish G1 round 1, then bail with 'b' during the rest.
    h1 = make_player(cass, keys=["enter", "enter", "b"])
    assert h1.play_group() is Event.BACK
    assert state_mod.STATE_FILE.exists()  # state saved at the round boundary

    # Session 2: fresh cassette + player, state re-applied.
    saved = load_state_data()
    h2 = make_player(cass, keys=["enter"] * 3)  # set 2, then all of G2
    pos = apply_state(h2.cassette, saved)
    g1 = h2.cassette.phases[0].groups[0]
    assert rounds_completed(g1) == 1  # round 1 restored from disk
    assert g1.exercises[0].sets[0].actual_reps == 10

    h2.player.jump_to(pos["phase_idx"], pos["group_idx"])
    h2.run()

    assert h2.player.is_complete()
    assert [s.actual_reps for s in g1.exercises[0].sets] == [10, 10]
    row = h2.cassette.phases[0].groups[1].exercises[0]
    assert row.sets[0].actual_reps == 5


def timed_group(*, name: str = "Plank", seconds: int = 3, rounds: int = 1, rest: int = 30) -> dict:
    return {
        "type": "straight", "rounds": rounds, "rest": rest,
        "exercises": [{"name": name, "timed": True, "reps": seconds}],
    }


# ---------------------------------------------------------------------------
# Back-navigation boundaries
# ---------------------------------------------------------------------------

def test_previous_group_at_first_group_returns_false(make_player):
    h = make_player(cassette(group(ex("A")), group(ex("B"))))

    assert h.player.previous_group() is False
    assert (h.player.phase_idx, h.player.group_idx) == (0, 0)  # playhead untouched


def test_back_at_first_group_bounces_and_replays(make_player, no_audio):
    """'b' on the very first transition must not crash or wrap to the end."""
    cass = cassette(group(ex("Squat", 10)))
    h = make_player(cass, keys=["b", "enter", "enter"])  # b bounces, then play

    h.run()

    assert h.player.is_complete()
    squat = h.cassette.phases[0].groups[0].exercises[0]
    assert squat.sets[0].actual_reps == 10
    # Performed exactly once despite the bounce: one set + one round chime.
    assert no_audio.sounds.count(b"set_complete") == 2


def test_back_chains_through_completed_groups(make_player, no_audio):
    """'b' from a completed-group redo screen keeps walking backwards."""
    cass = cassette(group(ex("A")), group(ex("B")), group(ex("C")))
    keys = (
        keys_to_finish(1) + keys_to_finish(1)  # G1, G2
        + ["b"]      # G3 transition -> back onto completed G2
        + ["b"]      # G2 redo screen -> further back onto completed G1
        + ["enter"]  # G1 redo screen -> continue (no redo)
        + keys_to_finish(1)  # forward lands on G3, the only incomplete group
    )
    h = make_player(cass, keys=keys)

    h.run()

    assert h.player.is_complete()
    for g in h.cassette.phases[0].groups:
        assert all(s.actual_reps is not None for s in all_sets(g))
        assert not g.skipped
    # Nothing was replayed: each group chimed exactly once (set + round each).
    assert no_audio.sounds.count(b"set_complete") == 6
    assert no_audio.sounds.count(b"group_complete") == 3


def test_arriving_at_complete_group_not_via_back_is_silent_done(make_player, no_audio):
    """Landing forward on an already-complete group: no screen, no fanfare."""
    cass = cassette(group(ex("A", 10)), group(ex("B", 5)))
    h = make_player(cass)
    complete_group(h.cassette.phases[0].groups[0])

    assert h.play_group() is Event.DONE

    assert h.keys.reads == 0  # no transition/redo screen was shown
    assert no_audio.sounds == []  # and no completion fanfare replayed
    assert not any("Next up" in line for line in no_audio.spoken)


def test_run_on_fully_complete_cassette_returns_immediately(make_player, no_audio):
    cass = cassette(group(ex("A")), group(ex("B")))
    h = make_player(cass)
    for g in h.cassette.phases[0].groups:
        complete_group(g)

    h.run()  # nothing left to play: no screens, no keys, no speech

    assert h.keys.reads == 0
    assert no_audio.spoken == []


# ---------------------------------------------------------------------------
# Skip/back during rest and timed holds
# ---------------------------------------------------------------------------

def test_skip_during_post_round_rest_saves_next_round(make_player):
    """Skip during the rest after round r must save round_idx = r + 1."""
    cass = cassette(group(ex("Squat", 10), rounds=2))
    h = make_player(cass, keys=["enter", "enter", "s"])  # 's' during the rest

    h.run()

    g = h.cassette.phases[0].groups[0]
    assert g.skipped
    assert [s.actual_reps for s in g.exercises[0].sets] == [10, None]  # round 1 kept
    assert load_state_data()["position"] == {"phase_idx": 0, "group_idx": 0, "round_idx": 1}
    assert h.player.is_complete()


def test_skip_during_restarted_pending_rest_saves_current_round(make_player):
    """Skip during the pending (restarted) rest saves round_idx as-is, not +1."""
    cass = cassette(group(ex("Squat", 10)), group(ex("Row", 5), rounds=2))
    h = make_player(cass)

    h.keys.push("enter", "enter")
    assert h.play_group() is Event.DONE  # G1
    assert h.player.advance_group()

    h.keys.push("enter", "enter", "b")  # G2: transition, set 1, 'b' during rest
    assert h.play_group() is Event.BACK
    assert h.player._pending_rest == {(0, 1)}

    assert h.player.previous_group()
    h.keys.push("enter")
    assert h.play_group(via_back=True) is Event.DONE  # G1 completed screen

    assert h.player.advance_group()  # forward onto G2: pending rest restarts
    h.keys.push("s")
    assert h.play_group() is Event.DONE  # 's' during the restarted rest

    g2 = h.cassette.phases[0].groups[1]
    assert g2.skipped
    assert g2.exercises[0].sets[0].actual_reps == 5  # round 1 progress kept
    # round_idx is the rounds already complete (1), not 1 + 1.
    assert load_state_data()["position"] == {"phase_idx": 0, "group_idx": 1, "round_idx": 1}


def test_back_during_restarted_pending_rest_rearms_it(make_player):
    """'b' out of the restarted rest must re-arm _pending_rest for next entry."""
    cass = cassette(group(ex("Squat", 10)), group(ex("Row", 5), rounds=2))
    h = make_player(cass)

    h.keys.push("enter", "enter")
    assert h.play_group() is Event.DONE  # G1
    assert h.player.advance_group()

    h.keys.push("enter", "enter", "b")  # G2: 'b' during the post-round rest
    assert h.play_group() is Event.BACK
    assert h.player._pending_rest == {(0, 1)}

    assert h.player.previous_group()
    h.keys.push("enter")
    assert h.play_group(via_back=True) is Event.DONE  # G1 completed screen
    assert h.player.advance_group()

    h.keys.push("b")  # 'b' again, during the *restarted* rest
    assert h.play_group() is Event.BACK
    assert h.player._pending_rest == {(0, 1)}  # re-armed, not dropped

    assert h.player.previous_group()
    h.keys.push("enter")
    assert h.play_group(via_back=True) is Event.DONE
    assert h.player.advance_group()

    h.keys.push("enter", "enter")  # end the (third) rest, then set 2
    assert h.play_group() is Event.DONE
    assert h.player._pending_rest == set()
    row = h.cassette.phases[0].groups[1].exercises[0]
    assert [s.actual_reps for s in row.sets] == [5, 5]  # set 2 played exactly once


def test_pending_rest_is_charged_in_every_screens_eta(make_player):
    """The ETA cliff corner: 'b' out of a rest leaves that rest owed (it
    restarts in full on re-entry), so while the user sits on any other
    group's screens the progress bar must still charge it — via the live
    ui.pending_rests binding the Player publishes at construction."""
    from exercise_coach import ui

    cass = cassette(group(ex("Row", 10)), group(ex("Squat", 10), rounds=2, rest=60))
    h = make_player(cass, keys=["enter", "enter"] + [""] * 40 + ["b"])
    h.player.jump_to(0, 1)

    ev = h.play_group()  # Squat: transition, set 1, 10s of the rest, then 'b'

    assert ev is Event.BACK
    assert ui.pending_rests is h.player._pending_rest  # live binding, not a copy
    assert (0, 1) in ui.pending_rests
    # What any other screen's progress bar now renders: Row's one set (30s
    # default pace) + Squat's set 2 (30s) + the owed 60s rest — the old
    # accounting saw only the two sets and under-stated by the full rest.
    assert "ETA: 2m 00s" in ui.build_progress_bar(h.cassette, 30.0)

    # Once the group is re-entered and finished, nothing stays owed.
    h.keys.push("enter", "enter")  # end the restarted rest, then set 2
    assert h.play_group(via_back=True) is Event.DONE
    assert ui.pending_rests == set()
    assert "ETA: 30s" in ui.build_progress_bar(h.cassette, 30.0)  # Row's set only


def test_skip_during_timed_hold_records_nothing(make_player, no_audio):
    cass = cassette(timed_group(seconds=30, rounds=2))
    h = make_player(cass, keys=["enter", "s"])  # transition, then 's' mid-hold

    h.run()

    g = h.cassette.phases[0].groups[0]
    assert g.skipped
    assert all(s.actual_reps is None for s in all_sets(g))  # hold not credited
    assert "Skipping Plank" in no_audio.spoken
    assert load_state_data()["position"] == {"phase_idx": 0, "group_idx": 0, "round_idx": 0}
    assert h.player.is_complete()


def test_back_during_timed_hold_restarts_it_on_return(make_player, no_audio):
    cass = cassette(group(ex("Squat", 10)), timed_group(seconds=3))
    keys = (
        keys_to_finish(1)       # G1
        + ["enter", "b"]        # G2: transition, 'b' mid-hold (not credited)
        + ["enter"]             # G1 completed screen: continue
        + ["enter"]             # G2 transition again; hold then elapses
    )
    h = make_player(cass, keys=keys)

    h.run()

    assert h.player.is_complete()
    plank = h.cassette.phases[0].groups[1].exercises[0]
    assert plank.sets[0].actual_reps == 3  # credited only by the full re-run
    # The get-in-position countdown ran twice: once before 'b', once after.
    assert no_audio.spoken.count("Get in position") == 2
    assert no_audio.spoken.count("Done") == 1


# ---------------------------------------------------------------------------
# Timed holds: fail path and early end (recommendations §6)
#
# Key-script arithmetic for holds: the get-in-position countdown consumes 12
# empty reads (3s at 0.25s/read); during the hold each empty read advances
# the fake clock 0.25s.
# ---------------------------------------------------------------------------

COUNTDOWN = [""] * 12


def test_hold_f_prompts_for_seconds_held(make_player):
    cass = cassette(timed_group(seconds=30))
    # 2s into the hold: 'f' ("I broke earlier"), then type 21 seconds.
    keys = ["enter"] + COUNTDOWN + [""] * 8 + ["f", "2", "1", "enter"]
    h = make_player(cass, keys=keys)

    h.run()

    plank = h.cassette.phases[0].groups[0].exercises[0]
    assert plank.sets[0].actual_reps == 21
    assert plank.sets[0].failure
    assert h.player.is_complete()
    assert load_state_data()["position"] == {"phase_idx": 0, "group_idx": 0, "round_idx": 1}


def test_hold_f_input_is_clamped_to_the_target_duration(make_player):
    cass = cassette(timed_group(seconds=30))
    keys = ["enter"] + COUNTDOWN + ["f", "9", "9", "enter"]  # 99 > 30
    h = make_player(cass, keys=keys)

    h.run()

    plank = h.cassette.phases[0].groups[0].exercises[0]
    assert plank.sets[0].actual_reps == 30  # clamped, like get_failure_reps
    assert plank.sets[0].failure


def test_hold_enter_early_counts_elapsed_as_a_shortfall(make_player, no_audio):
    cass = cassette(timed_group(seconds=30))
    keys = ["enter"] + COUNTDOWN + [""] * 40 + ["enter"]  # Enter at 10.0s
    h = make_player(cass, keys=keys)

    h.run()

    plank = h.cassette.phases[0].groups[0].exercises[0]
    assert plank.sets[0].actual_reps == 11  # the second in progress counts
    assert plank.sets[0].failure            # 11 < 30: a shortfall
    assert "Done" not in no_audio.spoken    # no full-hold voice line
    assert no_audio.sounds.count(b"set_complete") == 2  # set + round chime


def test_hold_enter_in_final_second_rounds_to_target(make_player, no_audio):
    """Documented boundary: ending during the final displayed second credits
    the full duration and is not a failure."""
    cass = cassette(timed_group(seconds=3))
    keys = ["enter"] + COUNTDOWN + [""] * 9 + ["enter"]  # Enter at 2.25s of 3
    h = make_player(cass, keys=keys)

    h.run()

    plank = h.cassette.phases[0].groups[0].exercises[0]
    assert plank.sets[0].actual_reps == 3
    assert not plank.sets[0].failure
    assert "Done" in no_audio.spoken


def test_hold_timer_running_out_is_unchanged(make_player, no_audio):
    """The untouched path: full hold, no failure, celebration intact."""
    cass = cassette(timed_group(seconds=3))
    h = make_player(cass, keys=["enter"])

    h.run()

    plank = h.cassette.phases[0].groups[0].exercises[0]
    assert plank.sets[0].actual_reps == 3
    assert not plank.sets[0].failure
    assert "Done" in no_audio.spoken


def test_single_set_jump_records_failed_hold_like_normal_play(make_player):
    """A set-level jump onto a hold honors the 'f' fail path too."""
    cass = cassette(timed_group(seconds=30, rounds=2, rest=2))
    keys = (
        ["j", "right", "down", "down", "enter"]   # jump menu: expand, pick set 2
        + COUNTDOWN + ["f", "1", "5", "enter"]    # set 2: broke at 15s
        + ["enter"]                               # back at set 1: transition
        + COUNTDOWN + [""] * 120                  # set 1: full 30s hold
        + ["enter"]                               # post-round rest
    )
    h = make_player(cass, keys=keys)

    h.run()

    plank = h.cassette.phases[0].groups[0].exercises[0]
    assert plank.sets[1].actual_reps == 15
    assert plank.sets[1].failure
    assert plank.sets[0].actual_reps == 30
    assert not plank.sets[0].failure
    assert h.player.is_complete()


def test_single_set_jump_hold_enter_early_records_a_shortfall(make_player, no_audio):
    """The play_single_set copy of the early-Enter hold recording (the
    play_group copy is covered by test_hold_enter_early_counts_elapsed_as_a_
    shortfall): a hold ended early via a set-level jump must credit only the
    seconds held and be marked a failure."""
    cass = cassette(timed_group(seconds=30, rounds=2, rest=2))
    keys = (
        ["j", "right", "down", "down", "enter"]   # jump menu: expand, pick set 2
        + COUNTDOWN + [""] * 40 + ["enter"]       # set 2: Enter at 10.0s
        + ["enter"]                               # back at set 1: transition
        + COUNTDOWN + [""] * 120                  # set 1: full 30s hold
        + ["enter"]                               # post-round rest
    )
    h = make_player(cass, keys=keys)

    h.run()

    plank = h.cassette.phases[0].groups[0].exercises[0]
    assert plank.sets[1].actual_reps == 11  # the second in progress counts
    assert plank.sets[1].failure            # 11 < 30: a shortfall
    assert plank.sets[0].actual_reps == 30
    assert not plank.sets[0].failure
    assert no_audio.spoken.count("Done") == 1  # only the full hold is voiced
    assert h.player.is_complete()


# ---------------------------------------------------------------------------
# play_cassette() top level
# ---------------------------------------------------------------------------

def test_play_cassette_full_run_logs_and_clears_state(no_audio, capsys):
    raw = cassette(group(ex("Squat", 10)), title="Full Run")
    raw["voice"] = {"session_intro": "Welcome.", "session_complete": "All done."}
    raw["context_exercises"] = [
        {"name": "Dead Hangs", "note": "Any time today.", "voice": "Hang around."},
    ]
    cass = load_cassette_from_dict(raw)
    clock = FakeClock()
    keys = ScriptedKeys(["enter", "enter", "enter"])  # context, transition, set

    play_cassette(cass, now=clock.now, read_key=keys, sleep=clock.sleep)

    # Session intro, context voice, session complete — in order.
    spoken = no_audio.spoken
    assert spoken.index("Welcome.") < spoken.index("Hang around.") < spoken.index("All done.")
    assert "Workout complete!" in capsys.readouterr().out
    # The log was written once and includes context + exercise lines...
    log = state_mod.LOG_FILE.read_text()
    assert log.count("--- Full Run") == 1
    assert "Dead Hangs" in log
    assert "Squat" in log
    # ...and the state file (written at the round boundary) was cleared, so a
    # re-run cannot resume-and-relog this workout.
    assert not state_mod.STATE_FILE.exists()


def test_play_cassette_already_complete_short_circuits(no_audio, capsys):
    """A finished cassette prints the log, saves it once, and clears state
    without ever starting playback (no TTY, no screens)."""
    cass = load_cassette_from_dict(cassette(group(ex("Squat", 10)), title="Done Run"))
    complete_group(cass.phases[0].groups[0])
    state_mod.save_state(cass, {"phase_idx": 0, "group_idx": 0, "round_idx": 1})

    def no_key():
        raise AssertionError("read_key must not be called on the short-circuit path")

    play_cassette(cass, read_key=no_key)

    assert "already complete" in capsys.readouterr().out
    log = state_mod.LOG_FILE.read_text()
    assert log.count("--- Done Run") == 1
    assert "Squat" in log
    assert not state_mod.STATE_FILE.exists()  # re-runs won't append more entries
    assert no_audio.spoken == []  # no session intro/complete voice either


# ---------------------------------------------------------------------------
# ETA ground truth: simulated playthrough vs estimate_remaining samples
# ---------------------------------------------------------------------------

def test_eta_tracks_simulated_time_remaining(make_player, monkeypatch):
    """Drive a full 3-round workout on the fake clock with every set taking
    exactly the 30s default and every rest running its full 60s, then compare
    every ETA the screens computed against the true simulated time left.

    This is the §6 cliff regression: the old accounting dropped the
    in-progress rest entirely (charging only `(rounds_left - 1) * rest`), so
    the moment a round completed the ETA fell 60s below the truth — outside
    this test's band. The estimate must also never be negative and never
    increase (the in-progress set is deliberately not live-credited, so the
    estimate is flat within a set: still non-increasing)."""
    from exercise_coach import ui

    cass = cassette(group(ex("Squat", 10), rounds=3, rest=60))
    one_set = [""] * 120 + ["enter"]   # 30.0s of hold-the-pose, then done
    one_rest = [""] * 240 + ["enter"]  # the full 60s rest, ended right at 0
    keys = ["enter"] + one_set + one_rest + one_set + one_rest + one_set
    h = make_player(cass, keys=keys)

    samples: list[tuple[float, int]] = []
    real_estimate = ui.estimate_remaining

    def spy(*args, **kwargs):
        est = real_estimate(*args, **kwargs)
        samples.append((h.clock.t, est))
        return est

    monkeypatch.setattr(ui, "estimate_remaining", spy)

    h.run()

    assert h.player.is_complete()
    end = h.clock.t
    assert end - 1_000_000.0 == 3 * 30 + 2 * 60  # the simulation itself: 210s
    assert len(samples) > 100  # every render sampled the ETA

    estimates = [est for _, est in samples]
    assert all(est >= 0 for est in estimates)
    assert all(b <= a for a, b in zip(estimates, estimates[1:], strict=False))  # non-increasing
    for t, est in samples:
        actual = end - t
        # Exact through the rests (the live countdown is charged); within a
        # set the estimate stays flat while truth ticks down, so it may sit
        # up to one 30s set-charge above the truth. ±1s of int truncation.
        assert actual - 1 <= est <= actual + 31, (t, est, actual)


# ---------------------------------------------------------------------------
# Learned pace persistence (recommendations §6: ETAs meaningful from set one)
# ---------------------------------------------------------------------------

def test_session_end_persists_median_pace_and_primes_next_session(no_audio):
    from exercise_coach import settings as user_settings
    from exercise_coach.ui import estimate_remaining

    # Pre-existing settings must survive the pace write untouched.
    user_settings.save_data({"volume": 0.5, "buddy": False})

    raw = cassette(group(ex("Squat", 10), rounds=3, rest=30), title="Pace Day")
    cass = load_cassette_from_dict(raw)
    clock = FakeClock()
    # Sets of 15s, 20s, and 40s (median 20); rests ended immediately.
    keys = ScriptedKeys(
        ["enter"]
        + [""] * 60 + ["enter"] + ["enter"]
        + [""] * 80 + ["enter"] + ["enter"]
        + [""] * 160 + ["enter"]
    )

    play_cassette(cass, now=clock.now, read_key=keys, sleep=clock.sleep)

    data = json.loads(user_settings.settings_file().read_text())
    assert data["paces"] == {"Squat": 20}      # the median, rounded
    assert data["volume"] == 0.5               # other keys untouched
    assert data["buddy"] is False
    assert user_settings.paces == {"Squat": 20}  # live global updated too

    # Next session: the learned pace drives the ETA from set one.
    user_settings.load_paces()
    fresh = load_cassette_from_dict(raw)
    assert estimate_remaining(fresh, 30.0, paces=user_settings.paces) == 3 * 20 + 2 * 30


def test_only_run_persists_paces_too(no_audio):
    from exercise_coach import settings as user_settings
    from exercise_coach.player import play_only_group

    cass = load_cassette_from_dict(cassette(group(ex("Row", 12))))
    clock = FakeClock()
    keys = ScriptedKeys(["enter"] + [""] * 48 + ["enter"])  # one 12s set

    play_only_group(cass, 0, 0, "Row", now=clock.now, read_key=keys, sleep=clock.sleep)

    assert user_settings.paces == {"Row": 12}
    assert json.loads(user_settings.settings_file().read_text())["paces"] == {"Row": 12}


def test_failure_prompt_time_is_excluded_from_learned_pace(make_player):
    """Time spent typing into the get_failure_reps prompt (un-pausable, no
    timer compensation) is thinking-and-typing, not set work: only the set
    screen's own 20s may enter the pace medians — the prompt idle time used
    to double the learned pace, in both play_group and play_single_set."""
    cass = cassette(group(ex("Squat", 10)))
    keys = (
        ["enter"]                       # transition
        + [""] * 80 + ["f"]             # a 20.0s set, then 'f'
        + [""] * 200 + ["8", "enter"]   # 20s idling in the prompt, then the count
    )
    h = make_player(cass, keys=keys)

    h.run()

    squat = h.cassette.phases[0].groups[0].exercises[0]
    assert squat.sets[0].actual_reps == 8
    assert squat.sets[0].failure
    assert h.player.rep_set_durations == [20.0]      # the set, not set + prompt
    assert h.player.session_paces() == {"Squat": 20}


def test_single_set_jump_failure_prompt_time_is_excluded_too(make_player):
    """Same guarantee on the play_single_set call site (set-level jump)."""
    cass = cassette(group(ex("Squat", 10), rounds=2, rest=2))
    keys = (
        ["j", "right", "down", "down", "enter"]  # jump menu: expand, pick set 2
        + [""] * 80 + ["f"]                      # a 20.0s set, then 'f'
        + [""] * 200 + ["7", "enter"]            # 20s in the prompt, then the count
        + ["enter", "enter", "enter"]            # set 1: transition, set, rest
    )
    h = make_player(cass, keys=keys)

    h.run()

    squat = h.cassette.phases[0].groups[0].exercises[0]
    assert squat.sets[1].actual_reps == 7
    assert squat.sets[1].failure
    assert h.player.rep_set_durations[0] == 20.0  # the jumped set, prompt excluded
    assert h.player.is_complete()


def test_hold_only_session_writes_no_paces(no_audio):
    """Timed holds know their own duration — they never learn a pace."""
    from exercise_coach import settings as user_settings

    cass = load_cassette_from_dict(cassette(timed_group(seconds=3)))
    clock = FakeClock()
    keys = ScriptedKeys(["enter"])

    play_cassette(cass, now=clock.now, read_key=keys, sleep=clock.sleep)

    assert user_settings.paces == {}
    assert "paces" not in user_settings.load_data()


# ---------------------------------------------------------------------------
# Robustness: explicit sets list shorter than rounds
# ---------------------------------------------------------------------------

def test_short_sets_list_is_padded_and_plays_through(make_player):
    cass = cassette(group(ex("Squat"), rounds=3))
    cass["phases"][0]["groups"][0]["exercises"][0]["sets"] = [{"reps": 12}]  # 1 < 3
    h = make_player(cass, keys=keys_to_finish(3))

    h.run()  # would IndexError on round 2 without the padding fix

    squat = h.cassette.phases[0].groups[0].exercises[0]
    assert len(squat.sets) == 3  # padded by repeating the last target
    assert [s.reps for s in squat.sets] == [12, 12, 12]
    assert [s.actual_reps for s in squat.sets] == [12, 12, 12]
    assert h.player.is_complete()
