"""Player state-machine tests: headless, scripted keys, fake clock.

Every test drives `Player` through `make_player` (see conftest): screens get
an injected read_key/now/sleep, audio is recorded instead of played, and
state files land in tmp_path.

Key-script arithmetic: a group needs 1 "enter" for its transition screen,
one per rep set, and one to end each rest (rests happen between rounds, so
rounds - 1 of them).
"""

from exercise_coach import state as state_mod
from exercise_coach.cassette import count_sets, rounds_completed
from exercise_coach.events import Event
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
