"""Jump navigation (PR B): menu model, overlay rendering, key driving,
player integration, and --only.

Hermetic like every other test: fake clock, scripted keys, recorded audio,
state/log files in tmp_path (conftest autouse fixtures).
"""

import json
from io import StringIO

import pytest
from conftest import FakeClock, ScriptedKeys
from rich.console import Console

from exercise_coach import cli
from exercise_coach import state as state_mod
from exercise_coach.cassette import load_cassette_from_dict, rounds_completed
from exercise_coach.jumpmenu import (
    JumpMenuState,
    JumpTarget,
    build_jump_rows,
    build_set_rows,
    match_only,
    render_jump_panel,
)
from exercise_coach.player import play_only_group
from exercise_coach.screens import jump_menu_screen
from exercise_coach.state import load_state_data

# ---------------------------------------------------------------------------
# Local cassette-dict builders (mirroring test_player's)
# ---------------------------------------------------------------------------


def ex(name: str, reps: int = 10, load: str = "") -> dict:
    return {"name": name, "reps": reps, "load": load}


def group(*exercises: dict, type: str = "straight", rounds: int = 1, rest: int = 30) -> dict:
    return {"type": type, "rounds": rounds, "rest": rest, "exercises": list(exercises)}


def cassette(*groups: dict, title: str = "Jump Test") -> dict:
    return {
        "version": "1.1",
        "meta": {"date": "2026-07-12", "title": title, "rest_default": 75},
        "phases": [{"type": "main", "groups": list(groups)}],
    }


def two_phase_cassette(warmup: list[dict], main: list[dict], title: str = "Jump Test") -> dict:
    return {
        "version": "1.1",
        "meta": {"date": "2026-07-12", "title": title, "rest_default": 75},
        "phases": [
            {"type": "warmup", "groups": warmup},
            {"type": "main", "groups": main},
        ],
    }


def complete_group(g) -> None:
    for e in g.exercises:
        for s in e.sets:
            s.actual_reps = s.reps


def render_to_text(renderable, width: int = 100) -> str:
    console = Console(file=StringIO(), record=True, width=width)
    console.print(renderable)
    return console.export_text()


# ---------------------------------------------------------------------------
# Menu model: rows and statuses
# ---------------------------------------------------------------------------


class TestJumpRows:
    def test_statuses_and_order_across_phases(self):
        raw = two_phase_cassette(
            [group(ex("Jacks", 20))],
            [
                group(ex("Squat", 10), rounds=3),
                group(ex("Bench", 8), ex("Row", 12), type="superset", rounds=2),
                group(ex("Curl", 12)),
            ],
        )
        c = load_cassette_from_dict(raw)
        complete_group(c.phases[0].groups[0])                              # done
        c.phases[1].groups[0].exercises[0].sets[0].actual_reps = 10        # 1/3
        c.phases[1].groups[1].skipped = True                               # skipped

        rows = build_jump_rows(c, 1, 2)  # playhead on Curl

        assert [(r.label, r.status) for r in rows] == [
            ("Jacks", "✓ done"),
            ("Squat", "1/3"),
            ("Bench + Row", "skipped"),
            ("Curl", "→ current"),
        ]
        # Positions follow cassette order across phases.
        assert [(r.phase_idx, r.group_idx) for r in rows] == [(0, 0), (1, 0), (1, 1), (1, 2)]
        # Redo eligibility: only the fully-recorded group.
        assert [r.completed for r in rows] == [True, False, False, False]

    def test_current_status_wins_over_done(self):
        c = load_cassette_from_dict(cassette(group(ex("Squat", 10))))
        complete_group(c.phases[0].groups[0])
        rows = build_jump_rows(c, 0, 0)
        assert rows[0].status == "→ current"
        assert rows[0].completed  # still redo-eligible

    def test_fresh_group_shows_zero_of_rounds(self):
        c = load_cassette_from_dict(cassette(group(ex("Squat", 10), rounds=3)))
        assert build_jump_rows(c, -1, -1)[0].status == "0/3"


class TestSetRows:
    def test_superset_rows_are_round_major_with_markers(self):
        raw = cassette(group(ex("Bench", 8), ex("Row", 12), type="superset", rounds=2))
        c = load_cassette_from_dict(raw)
        g = c.phases[0].groups[0]
        g.exercises[0].sets[0].actual_reps = 8
        g.exercises[1].sets[0].actual_reps = 5
        g.exercises[1].sets[0].failure = True

        rows = build_set_rows(0, 0, g)

        assert [(r.label, r.status) for r in rows] == [
            ("set 1 · Bench ×8", "✓ 8"),
            ("set 1 · Row ×12", "✗ 5 (failed)"),
            ("set 2 · Bench ×8", ""),
            ("set 2 · Row ×12", ""),
        ]
        assert [(r.ex_idx, r.round_idx) for r in rows] == [(0, 0), (1, 0), (0, 1), (1, 1)]
        assert [r.completed for r in rows] == [True, True, False, False]

    def test_timed_sets_show_seconds_suffix(self):
        raw = cassette({
            "type": "straight", "rounds": 1, "rest": 30,
            "exercises": [{"name": "Plank", "timed": True, "reps": 30}],
        })
        c = load_cassette_from_dict(raw)
        rows = build_set_rows(0, 0, c.phases[0].groups[0])
        assert rows[0].label == "set 1 · Plank ×30s"

    def test_hold_statuses_show_seconds_not_reps(self):
        """A recorded hold's actuals are seconds: '✗ 21s (failed)' / '✓ 30s'
        (rep-set statuses stay bare, as asserted above)."""
        raw = cassette({
            "type": "straight", "rounds": 2, "rest": 30,
            "exercises": [{"name": "Plank", "timed": True, "reps": 30}],
        })
        c = load_cassette_from_dict(raw)
        g = c.phases[0].groups[0]
        g.exercises[0].sets[0].actual_reps = 21
        g.exercises[0].sets[0].failure = True
        g.exercises[0].sets[1].actual_reps = 30

        rows = build_set_rows(0, 0, g)

        assert rows[0].status == "✗ 21s (failed)"
        assert rows[1].status == "✓ 30s"
        assert [r.completed for r in rows] == [True, True]


# ---------------------------------------------------------------------------
# Menu state machine (pure key driving)
# ---------------------------------------------------------------------------


def three_group_state(cur: int = 0) -> JumpMenuState:
    c = load_cassette_from_dict(cassette(group(ex("A")), group(ex("B")), group(ex("C"))))
    return JumpMenuState(c, 0, cur)


class TestJumpMenuState:
    def test_cursor_starts_on_current_row(self):
        state = three_group_state(cur=1)
        assert state.cursor == 1

    def test_arrows_and_kj_move_with_clamping(self):
        state = three_group_state()
        assert state.handle_key("up") is None
        assert state.cursor == 0  # clamped at top
        state.handle_key("down")
        state.handle_key("j")  # 'j' means down inside the menu
        assert state.cursor == 2
        state.handle_key("down")
        assert state.cursor == 2  # clamped at bottom
        state.handle_key("k")
        state.handle_key("up")
        assert state.cursor == 0

    def test_enter_jumps_to_cursor_group(self):
        state = three_group_state()
        state.handle_key("down")
        kind, target = state.handle_key("enter")
        assert kind == "jump"
        assert (target.phase_idx, target.group_idx) == (0, 1)
        assert target.round_idx is None and not target.redo

    def test_digit_entry_then_enter_jumps_to_that_row(self):
        state = three_group_state()
        state.handle_key("3")
        kind, target = state.handle_key("enter")
        assert kind == "jump"
        assert (target.phase_idx, target.group_idx) == (0, 2)

    def test_invalid_row_number_is_ignored(self):
        state = three_group_state()
        state.handle_key("9")
        assert state.handle_key("enter") is None  # menu stays open
        assert state.digits == ""  # buffer cleared for a retry
        # Backspace edits the buffer (get_failure_reps pattern).
        state.handle_key("1")
        state.handle_key("2")
        state.handle_key("\x7f")
        assert state.digits == "1"

    def test_expand_navigate_and_select_set(self):
        raw = cassette(
            group(ex("Bench", 8), ex("Row", 12), type="superset", rounds=1),
            group(ex("Squat", 10)),
        )
        state = JumpMenuState(load_cassette_from_dict(raw), 0, 0)
        state.handle_key("right")  # expand Bench + Row
        assert state.expanded == 0 and state.cursor == 0
        state.handle_key("down")
        state.handle_key("down")  # onto Row's set row
        kind, target = state.handle_key("enter")
        assert kind == "jump"
        assert (target.ex_idx, target.round_idx) == (1, 0)
        assert (target.phase_idx, target.group_idx) == (0, 0)

    def test_l_expands_and_left_collapses_back_to_group(self):
        raw = cassette(group(ex("Bench", 8), ex("Row", 12), type="superset", rounds=1))
        state = JumpMenuState(load_cassette_from_dict(raw), 0, 0)
        state.handle_key("l")
        state.handle_key("down")  # into the set rows
        state.handle_key("left")
        assert state.expanded is None
        assert state.cursor == 0  # back on the parent group row

    def test_enter_on_completed_set_is_a_no_op(self):
        raw = cassette(group(ex("Bench", 8), ex("Row", 12), type="superset", rounds=1))
        c = load_cassette_from_dict(raw)
        c.phases[0].groups[0].exercises[0].sets[0].actual_reps = 8
        state = JumpMenuState(c, 0, 0)
        state.handle_key("right")
        state.handle_key("down")  # Bench's recorded set
        # Replaying a recorded set would overwrite it; redo is the only
        # destructive action, so this is not a target.
        assert state.handle_key("enter") is None

    def test_redo_requires_y_confirmation(self):
        c = load_cassette_from_dict(cassette(group(ex("A")), group(ex("B"))))
        complete_group(c.phases[0].groups[0])
        state = JumpMenuState(c, 0, 1)
        state.handle_key("k")  # up onto the completed A
        assert state.handle_key("r") is None
        assert state.confirm == 0
        assert state.handle_key("x") is None  # anything but 'y' backs out
        assert state.confirm is None
        state.handle_key("r")
        kind, target = state.handle_key("y")
        assert kind == "redo"
        assert target.redo and (target.phase_idx, target.group_idx) == (0, 0)

    def test_redo_ignored_on_incomplete_group(self):
        state = three_group_state()
        assert state.handle_key("r") is None
        assert state.confirm is None

    def test_non_ascii_digit_is_ignored_not_a_crash(self):
        """'²'.isdigit() is True but int('²') raises ValueError — the menu
        must ignore such keys instead of crashing the whole TUI on Enter."""
        state = three_group_state()
        assert state.handle_key("²") is None
        assert state.digits == ""  # never buffered
        kind, target = state.handle_key("enter")  # still selects the cursor row
        assert kind == "jump"
        assert (target.phase_idx, target.group_idx) == (0, 0)

    @pytest.mark.parametrize("key", ["esc", "q"])
    def test_cancel_keys(self, key):
        state = three_group_state()
        assert state.handle_key(key) == ("cancel", None)


# ---------------------------------------------------------------------------
# Overlay renderer (Rich snapshot substrings)
# ---------------------------------------------------------------------------


class TestRenderJumpPanel:
    def make_state(self) -> JumpMenuState:
        raw = two_phase_cassette(
            [group(ex("Jacks", 20))],
            [group(ex("Bench", 8), ex("Row", 12), type="superset", rounds=2),
             group(ex("Curl", 12), rounds=3)],
        )
        c = load_cassette_from_dict(raw)
        complete_group(c.phases[0].groups[0])
        return JumpMenuState(c, 1, 1)

    def test_rows_numbers_statuses_and_cursor(self):
        state = self.make_state()
        text = render_to_text(render_jump_panel(state))
        assert "Jump to" in text
        assert "1. Jacks" in text
        assert "✓ done" in text
        assert "2. Bench + Row" in text
        assert "0/2" in text
        assert "3. Curl" in text
        assert "→ current" in text
        # Cursor marker sits on the current row.
        assert "❯  3. Curl" in text

    def test_expanded_set_rows_render(self):
        state = self.make_state()
        state.handle_key("k")      # onto Bench + Row
        state.handle_key("right")  # expand
        text = render_to_text(render_jump_panel(state))
        assert "set 1 · Bench ×8" in text
        assert "set 2 · Row ×12" in text

    def test_digit_buffer_renders(self):
        state = self.make_state()
        state.handle_key("2")
        assert "Go to row: 2_" in render_to_text(render_jump_panel(state))

    def test_redo_confirmation_renders_what_will_be_cleared(self):
        state = self.make_state()
        state.handle_key("k")
        state.handle_key("k")  # onto the completed Jacks
        state.handle_key("r")
        text = render_to_text(render_jump_panel(state))
        assert "Redo Jacks?" in text
        assert "Clears 1 recorded set(s)" in text
        assert "y = yes" in text


# ---------------------------------------------------------------------------
# jump_menu_screen (run_screen driving)
# ---------------------------------------------------------------------------


class TestJumpMenuScreen:
    def screen(self, make_player, keys):
        h = make_player(
            cassette(group(ex("A")), group(ex("B")), group(ex("C"))), keys=keys,
        )
        return jump_menu_screen(
            h.live, h.cassette, 0, 0,
            now=h.clock.now, read_key=h.keys, sleep=h.clock.sleep,
        )

    def test_arrow_down_and_enter_select_second_group(self, make_player):
        target = self.screen(make_player, ["down", "enter"])
        assert (target.phase_idx, target.group_idx) == (0, 1)

    def test_j_moves_down_inside_the_menu(self, make_player):
        target = self.screen(make_player, ["j", "j", "enter"])
        assert (target.phase_idx, target.group_idx) == (0, 2)

    def test_esc_cancels(self, make_player):
        assert self.screen(make_player, ["esc"]) is None

    def test_digits_then_enter(self, make_player):
        target = self.screen(make_player, ["2", "enter"])
        assert (target.phase_idx, target.group_idx) == (0, 1)


# ---------------------------------------------------------------------------
# Player integration
# ---------------------------------------------------------------------------


class TestPlayerJump:
    def test_jump_from_transition_out_of_order_completion(self, make_player, no_audio):
        """Do group 3 first; on completion the player advances to group 1
        (the next incomplete group in cassette order — the handoff doc's
        jumped-out-of-order advance case)."""
        cass = cassette(group(ex("A")), group(ex("B")), group(ex("C")))
        keys = ["j", "3", "enter"] + ["enter"] * 6
        h = make_player(cass, keys=keys)

        h.run()

        assert h.player.is_complete()
        # A's transition announced itself before 'j' was pressed; after the
        # jump the play order is C, then back to A, then B.
        next_ups = [s for s in no_audio.spoken if s.startswith("Next up: ")]
        assert next_ups == ["Next up: A", "Next up: C", "Next up: A", "Next up: B"]

    def test_jump_preserves_progress_and_resumes_mid_group(self, make_player, no_audio):
        """Jump away mid-superset; completed sets stay completed, and coming
        back resumes at the first incomplete set (Bench never replays)."""
        cass = cassette(
            group(ex("Bench", 8), ex("Row", 12), type="superset"),
            group(ex("Squat", 10)),
        )
        keys = [
            "enter", "enter",        # G1 transition + Bench set
            "j", "down", "enter",    # 'j' during Row's set -> jump to G2
            "enter", "enter",        # G2 completes
            "enter", "enter",        # back on G1: transition, then Row only
        ]
        h = make_player(cass, keys=keys)

        h.run()

        assert h.player.is_complete()
        bench, row = h.cassette.phases[0].groups[0].exercises
        assert bench.sets[0].actual_reps == 8
        assert row.sets[0].actual_reps == 12
        # Bench performed exactly once: Bench 1 + G2 (set+round) 2 + Row 1 +
        # G1 round 1 = 5 set chimes. A replayed Bench would add one.
        assert no_audio.sounds.count(b"set_complete") == 5
        assert no_audio.sounds.count(b"group_complete") == 2
        # The Next-up order proves the jump actually happened mid-set: with
        # 'j' dead on the rep-set screen the run would collapse to
        # [Bench and Row, Squat] and still satisfy the counts above.
        next_ups = [s for s in no_audio.spoken if s.startswith("Next up: ")]
        assert next_ups == [
            "Next up: Bench and Row", "Next up: Squat", "Next up: Bench and Row",
        ]

    def test_cancel_resumes_without_transition_or_intro_replay(self, make_player, no_audio):
        cass = cassette(group(ex("Squat", 10), rounds=2, rest=1))
        keys = [
            "enter",       # transition
            "j", "esc",    # menu from set 1, cancelled
            "enter",       # set 1 (resumed directly — no transition replay)
            "enter",       # rest
            "enter",       # set 2
        ]
        h = make_player(cass, keys=keys)

        h.run()

        assert h.player.is_complete()
        assert no_audio.spoken.count("Next up: Squat") == 1

    def test_cancel_from_transition_reshows_the_setup_screen(self, make_player, no_audio):
        """From the setup screen nothing has started: cancelling the menu
        lands back on the setup screen (not in set 1)."""
        cass = cassette(group(ex("Squat", 10)))
        keys = ["j", "esc", "enter", "enter"]  # menu, cancel, transition, set
        h = make_player(cass, keys=keys)

        h.run()

        assert h.player.is_complete()
        # The transition screen re-announced itself after the cancel.
        assert no_audio.spoken.count("Next up: Squat") == 2

    def test_set_level_jump_superset_plays_exactly_one_set(self, make_player, no_audio):
        """Enter on a set row plays that one exercise's set (not the whole
        superset round), records it, then returns to the normal flow."""
        cass = cassette(
            group(ex("Bench", 8), ex("Row", 12), type="superset"),
            group(ex("Squat", 10)),
        )
        keys = [
            "j", "right", "down", "down", "enter",  # expand G1, select Row's set
            "enter",                                # play just Row's set
            "enter", "enter",                       # normal flow resumes at G2
            "enter", "enter",                       # wraps to G1: Bench only
        ]
        h = make_player(cass, keys=keys)

        h.run()

        assert h.player.is_complete()
        bench, row = h.cassette.phases[0].groups[0].exercises
        assert row.sets[0].actual_reps == 12
        assert bench.sets[0].actual_reps == 8
        # Row 1 + G2 (set+round) 2 + Bench 1 + G1 round 1 = 5.
        assert no_audio.sounds.count(b"set_complete") == 5
        # After the single set the flow went to G2 before revisiting G1.
        spoken = no_audio.spoken
        assert spoken.index("Next up: Squat") < spoken.index("Next up: Bench and Row", 1)

    def test_play_single_set_records_failure_and_saves_state(self, make_player):
        cass = cassette(group(ex("Squat", 10), rounds=2))
        h = make_player(cass, keys=["f", "7", "enter"])

        h.player.play_single_set(h.live, JumpTarget(0, 0, ex_idx=0, round_idx=1))

        squat = h.cassette.phases[0].groups[0].exercises[0]
        assert squat.sets[1].actual_reps == 7
        assert squat.sets[1].failure
        assert squat.sets[0].actual_reps is None  # set 1 untouched
        saved = load_state_data()
        # Round 1 is still incomplete, so the resume round is 0.
        assert saved["position"] == {"phase_idx": 0, "group_idx": 0, "round_idx": 0}
        assert saved["groups_state"][0]["sets"][0][1] == {"actual_reps": 7, "failure": True}

    def test_single_set_abandoned_records_nothing(self, make_player):
        cass = cassette(group(ex("Squat", 10), rounds=2))
        h = make_player(cass, keys=["b"])

        h.player.play_single_set(h.live, JumpTarget(0, 0, ex_idx=0, round_idx=0))

        squat = h.cassette.phases[0].groups[0].exercises[0]
        assert all(s.actual_reps is None for s in squat.sets)
        assert not state_mod.STATE_FILE.exists()

    def test_menu_redo_confirms_and_clears_only_that_group(self, make_player):
        cass = cassette(group(ex("Press", 10)), group(ex("Curl", 12)))
        h = make_player(cass)
        press = h.cassette.phases[0].groups[0].exercises[0]
        press.sets[0].actual_reps = 5  # completed at 5 (below target)

        # Playhead starts past the done Press, on Curl's transition. In the
        # menu: up onto Press, 'r', a stray key (confirmation backs out),
        # 'r' again, 'y' (confirmed) -> Press replays clean.
        h.keys.push("j", "k", "r", "x", "r", "y",
                    "enter", "enter",   # Press transition + set
                    "enter", "enter")   # Curl
        h.run()

        assert h.player.is_complete()
        assert press.sets[0].actual_reps == 10  # replayed at target
        curl = h.cassette.phases[0].groups[1].exercises[0]
        assert curl.sets[0].actual_reps == 12   # the other group untouched

    def test_jump_away_during_rest_restarts_rest_on_return(self, make_player, no_audio):
        """Jumping away mid-rest arms the pending rest (same as BACK): the
        rest restarts when the group is re-entered mid-round."""
        cass = cassette(
            group(ex("Squat", 10), rounds=2, rest=2),
            group(ex("Row", 12)),
        )
        keys = (
            ["enter", "enter"]        # G1 transition + set 1
            + ["j", "2", "enter"]     # 'j' during the rest -> jump to G2
            + ["enter", "enter"]      # G2 completes
            + [""] * 12               # restarted rest elapses (fake clock)
            + ["enter", "enter"]      # end rest, set 2
        )
        h = make_player(cass, keys=keys)

        h.run()

        assert h.player.is_complete()
        # The rest-done chime can only fire when a rest counts past zero —
        # the first rest was interrupted immediately, so this proves the
        # restart happened on return.
        assert no_audio.sounds.count(b"rest_done") == 1
        squat = h.cassette.phases[0].groups[0].exercises[0]
        assert [s.actual_reps for s in squat.sets] == [10, 10]

    def test_j_is_unmapped_inside_a_timed_hold(self, make_player):
        cass = cassette({
            "type": "straight", "rounds": 1, "rest": 30,
            "exercises": [{"name": "Plank", "timed": True, "reps": 3}],
        })
        # 'j' mid-hold is ignored; the hold simply elapses and is credited.
        h = make_player(cass, keys=["enter", "j", "j"])

        h.run()

        assert h.player.is_complete()
        plank = h.cassette.phases[0].groups[0].exercises[0]
        assert plank.sets[0].actual_reps == 3

    def test_jump_from_completed_group_screen(self, make_player, no_audio):
        """'j' on the back-onto-completed-group screen (its hint advertises
        it) opens the menu; the Next-up order proves the jump happened."""
        cass = cassette(group(ex("A")), group(ex("B")), group(ex("C")))
        keys = [
            "enter", "enter",   # A plays
            "b",                # from B's setup back onto the completed A
            "j", "3", "enter",  # jump menu from the completed screen -> C
            "enter", "enter",   # C plays
            "enter", "enter",   # then the next incomplete group: B
        ]
        h = make_player(cass, keys=keys)

        h.run()

        assert h.player.is_complete()
        next_ups = [s for s in no_audio.spoken if s.startswith("Next up: ")]
        # With 'j' dead on that screen the order collapses to A, B, B, C.
        assert next_ups == ["Next up: A", "Next up: B", "Next up: C", "Next up: B"]

    def test_prerecorded_round_gets_no_phantom_round_complete(self, make_player, no_audio):
        """A round whose sets were all recorded earlier (set-level jump) is
        passed over silently on normal play: no round announcement, no extra
        chime/save, and no second back-to-back rest."""
        g = group(ex("Squat", 10), rounds=3, rest=2)
        g["voice_round_complete"] = ["Round 1 done.", "Round 2 done."]
        h = make_player(cassette(g), keys=["enter", "enter", "enter", "enter"])
        # Round 2 (index 1) was recorded earlier by a set-level jump.
        h.cassette.phases[0].groups[0].exercises[0].sets[1].actual_reps = 10

        h.run()  # transition, set r0, one rest, set r2 — exactly four keys

        assert h.player.is_complete()
        assert no_audio.spoken.count("Round 1 done.") == 1
        assert "Round 2 done." not in no_audio.spoken
        # r0 set + r0 round + r2 set + r2 round = 4; a phantom round-1
        # completion would add a fifth chime (and a second rest).
        assert no_audio.sounds.count(b"set_complete") == 4

    def test_two_interrupted_rests_are_both_owed(self, make_player, no_audio):
        """Interrupting B's rest must not cancel A's still-pending rest: each
        group's cut-short rest restarts on its own re-entry."""
        cass = cassette(
            group(ex("A", 10), rounds=2, rest=2),
            group(ex("B", 12), rounds=2, rest=2),
        )
        keys = (
            ["enter", "enter"]       # A transition + set 1
            + ["j", "2", "enter"]    # 'j' during A's rest -> jump to B
            + ["enter", "enter"]     # B transition + set 1
            + ["b"]                  # 'b' during B's rest -> back to A
            + [""] * 12 + ["enter"]  # A's restarted rest elapses, then ends
            + ["enter"]              # A set 2 -> A complete -> on to B
            + [""] * 12 + ["enter"]  # B's restarted rest elapses, then ends
            + ["enter"]              # B set 2
        )
        h = make_player(cass, keys=keys)

        h.run()

        assert h.player.is_complete()
        # Both restarted rests counted past zero. With a single pending-rest
        # slot, B's interruption would erase A's owed rest (one ding only,
        # and A's set 2 would start with no rest at all).
        assert no_audio.sounds.count(b"rest_done") == 2

    def test_single_set_jump_completing_group_fires_group_fanfare(
        self, make_player, no_audio,
    ):
        """When a set-level jump records the group's last missing slot, the
        group-complete voice/chime/celebration fire exactly as on normal
        completion (play_group's silent already-complete re-entry is not the
        only path anymore)."""
        g = group(ex("Bench", 8), ex("Row", 12), type="superset")
        g["voice_group_complete"] = "Superset complete."
        h = make_player(cassette(g), keys=["enter"])
        h.cassette.phases[0].groups[0].exercises[0].sets[0].actual_reps = 8  # Bench done

        h.player.play_single_set(h.live, JumpTarget(0, 0, ex_idx=1, round_idx=0))

        assert no_audio.sounds.count(b"group_complete") == 1
        assert "Superset complete." in no_audio.spoken

    def test_single_set_jump_not_completing_group_has_no_group_fanfare(
        self, make_player, no_audio,
    ):
        cass = cassette(group(ex("Squat", 10), rounds=2))
        h = make_player(cass, keys=["enter"])

        h.player.play_single_set(h.live, JumpTarget(0, 0, ex_idx=0, round_idx=0))

        assert b"group_complete" not in no_audio.sounds

    def test_out_of_order_completion_via_set_jump_celebrates_once(
        self, make_player, no_audio,
    ):
        """Full-run version: finishing the cassette through a set-level jump
        plays the group fanfare exactly once (not zero, not doubled by the
        play_group re-entry)."""
        g = group(ex("Bench", 8), ex("Row", 12), type="superset")
        g["voice_group_complete"] = "Superset complete."
        keys = [
            "enter", "enter",                       # transition + Bench
            "j", "right", "down", "down", "enter",  # set-level jump to Row
            "enter",                                # perform Row -> all done
        ]
        h = make_player(cassette(g), keys=keys)

        h.run()

        assert h.player.is_complete()
        assert no_audio.sounds.count(b"group_complete") == 1
        assert no_audio.spoken.count("Superset complete.") == 1

    def test_abandoned_single_set_detour_keeps_group_skipped(self, make_player):
        """Abandoning the detour ('b' mid-set) records nothing AND leaves an
        explicit skip in force — the group must not sneak back into the
        advance order."""
        cass = cassette(group(ex("A", 10)), group(ex("B", 12)))
        h = make_player(cass, keys=["b"])
        a = h.cassette.phases[0].groups[0]
        a.skipped = True

        h.player.play_single_set(h.live, JumpTarget(0, 0, ex_idx=0, round_idx=0))

        assert a.skipped
        assert a.exercises[0].sets[0].actual_reps is None

    def test_performed_single_set_revives_skipped_group(self, make_player):
        cass = cassette(group(ex("A", 10), rounds=2))
        h = make_player(cass, keys=["enter"])
        a = h.cassette.phases[0].groups[0]
        a.skipped = True

        h.player.play_single_set(h.live, JumpTarget(0, 0, ex_idx=0, round_idx=0))

        assert not a.skipped
        assert a.exercises[0].sets[0].actual_reps == 10

    def test_jump_to_skipped_group_makes_it_playable(self, make_player):
        cass = cassette(group(ex("A")), group(ex("B")))
        h = make_player(cass)
        h.cassette.phases[0].groups[1].skipped = True

        # From A's transition jump to the skipped B; it plays normally.
        h.keys.push("j", "2", "enter", "enter", "enter",  # B transition + set
                    "enter", "enter")                     # back to A
        h.run()

        assert h.player.is_complete()
        b = h.cassette.phases[0].groups[1]
        assert not b.skipped
        assert b.exercises[0].sets[0].actual_reps == 10


# ---------------------------------------------------------------------------
# --only: matching
# ---------------------------------------------------------------------------


class TestMatchOnly:
    def rows(self):
        c = load_cassette_from_dict(cassette(
            group(ex("Goblet Squat", 10)),
            group(ex("Bench", 8), ex("Row", 12), type="superset"),
            group(ex("Squat Jump", 15)),
        ))
        return build_jump_rows(c, -1, -1)

    def test_substring_is_case_insensitive(self):
        (m,) = match_only(self.rows(), "beNCh")
        assert m.label == "Bench + Row"

    def test_index_is_one_based_menu_order(self):
        (m,) = match_only(self.rows(), "3")
        assert m.label == "Squat Jump"

    def test_out_of_range_index_matches_nothing(self):
        assert match_only(self.rows(), "4") == []
        assert match_only(self.rows(), "0") == []

    def test_ambiguous_substring_returns_all_candidates(self):
        assert len(match_only(self.rows(), "squat")) == 2

    def test_no_match(self):
        assert match_only(self.rows(), "deadlift") == []

    def test_non_ascii_digit_is_not_an_index(self):
        # '²'.isdigit() is True but int('²') raises ValueError; the spec must
        # fall through to (failed) substring matching, not crash the CLI.
        assert match_only(self.rows(), "²") == []


# ---------------------------------------------------------------------------
# --only: CLI wiring
# ---------------------------------------------------------------------------


def write_cassette(tmp_path, raw, name="workout.json"):
    p = tmp_path / name
    p.write_text(json.dumps(raw))
    return p


def run_main(monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["coach", *argv])
    cli.main()


def forbid_input(monkeypatch):
    def boom():
        raise AssertionError("input() must not be called on this path")
    monkeypatch.setattr("builtins.input", boom)


@pytest.fixture
def only_played(monkeypatch):
    """Replace cli.play_only_group with a recorder of (pi, gi, label)."""
    calls = []

    def _record(cassette, phase_idx, group_idx, label, **_kwargs):
        calls.append((phase_idx, group_idx, label))

    monkeypatch.setattr(cli, "play_only_group", _record)
    return calls


TWO_GROUPS = cassette(
    group(ex("Goblet Squat", 10)),
    group(ex("Bench", 8), ex("Row", 12), type="superset"),
    title="Only Day",
)


class TestOnlyCli:
    def test_only_by_substring(self, only_played, tmp_path, monkeypatch):
        forbid_input(monkeypatch)
        file = write_cassette(tmp_path, TWO_GROUPS)
        run_main(monkeypatch, str(file), "--only", "row")
        assert only_played == [(0, 1, "Bench + Row")]

    def test_only_by_index(self, only_played, tmp_path, monkeypatch):
        forbid_input(monkeypatch)
        file = write_cassette(tmp_path, TWO_GROUPS)
        run_main(monkeypatch, str(file), "--only", "1")
        assert only_played == [(0, 0, "Goblet Squat")]

    def test_no_match_exits_2_and_lists_groups(self, only_played, tmp_path, monkeypatch, capsys):
        forbid_input(monkeypatch)
        file = write_cassette(tmp_path, TWO_GROUPS)
        with pytest.raises(SystemExit) as exc:
            run_main(monkeypatch, str(file), "--only", "deadlift")
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "No group matches" in err
        assert "1. Goblet Squat" in err
        assert "2. Bench + Row" in err
        assert only_played == []

    def test_unicode_digit_spec_exits_2_not_traceback(
        self, only_played, tmp_path, monkeypatch, capsys,
    ):
        """--only '²' (AltGr+2 on many European layouts) must get the friendly
        exit-2 menu, not a ValueError traceback."""
        forbid_input(monkeypatch)
        file = write_cassette(tmp_path, TWO_GROUPS)
        with pytest.raises(SystemExit) as exc:
            run_main(monkeypatch, str(file), "--only", "²")
        assert exc.value.code == 2
        assert "No group matches" in capsys.readouterr().err
        assert only_played == []

    def test_ambiguous_match_exits_2(self, only_played, tmp_path, monkeypatch, capsys):
        forbid_input(monkeypatch)
        raw = cassette(group(ex("Squat", 10)), group(ex("Squat Jump", 15)))
        file = write_cassette(tmp_path, raw)
        with pytest.raises(SystemExit) as exc:
            run_main(monkeypatch, str(file), "--only", "squat")
        assert exc.value.code == 2
        assert "Multiple groups match" in capsys.readouterr().err
        assert only_played == []

    def test_only_never_offers_resume_or_touches_state(
        self, only_played, tmp_path, monkeypatch
    ):
        """A saved full-session state (even a mismatching one that try_resume
        would clear) survives an --only run byte-for-byte."""
        forbid_input(monkeypatch)  # also proves no resume prompt appears
        other = load_cassette_from_dict(cassette(group(ex("Press", 5)), title="Full"))
        other.phases[0].groups[0].exercises[0].sets[0].actual_reps = 5
        state_mod.save_state(other, {"phase_idx": 0, "group_idx": 0, "round_idx": 1})
        before = state_mod.STATE_FILE.read_bytes()

        file = write_cassette(tmp_path, TWO_GROUPS)
        run_main(monkeypatch, str(file), "--only", "bench")

        assert only_played == [(0, 1, "Bench + Row")]
        assert state_mod.STATE_FILE.read_bytes() == before


# ---------------------------------------------------------------------------
# --only: playback + partial log + state isolation
# ---------------------------------------------------------------------------


class TestPlayOnlyGroup:
    def play(self, raw, pi, gi, label, keys):
        c = load_cassette_from_dict(raw)
        clock = FakeClock()
        play_only_group(
            c, pi, gi, label,
            now=clock.now, read_key=ScriptedKeys(keys), sleep=clock.sleep,
        )
        return c

    def test_plays_the_group_and_logs_partial(self, no_audio, capsys):
        raw = cassette(
            group(ex("Squat", 10)),
            group(ex("Row", 12), rounds=2, rest=2),
            title="Partial Day",
        )
        c = self.play(raw, 0, 1, "Row", ["enter", "enter", "enter", "enter"])

        row = c.phases[0].groups[1].exercises[0]
        assert [s.actual_reps for s in row.sets] == [12, 12]
        # The other group was never touched or played.
        assert c.phases[0].groups[0].exercises[0].sets[0].actual_reps is None

        log = state_mod.LOG_FILE.read_text()
        assert log.count("--- Partial Day (partial: Row)") == 1
        assert "Row" in log
        assert "Squat" not in log  # body lists only the played group
        assert "Row" in capsys.readouterr().out

    def test_never_creates_a_state_file(self, no_audio):
        self.play(cassette(group(ex("Squat", 10))), 0, 0, "Squat", ["enter", "enter"])
        assert not state_mod.STATE_FILE.exists()

    def test_existing_state_file_is_byte_identical_after_run(self, no_audio):
        other = load_cassette_from_dict(cassette(group(ex("Press", 5)), title="Full"))
        other.phases[0].groups[0].exercises[0].sets[0].actual_reps = 5
        state_mod.save_state(other, {"phase_idx": 0, "group_idx": 0, "round_idx": 1})
        before = state_mod.STATE_FILE.read_bytes()

        self.play(cassette(group(ex("Squat", 10))), 0, 0, "Squat", ["enter", "enter"])

        assert state_mod.STATE_FILE.read_bytes() == before

    def test_back_and_jump_bounce_within_the_group(self, no_audio):
        """'b' and 'j' have nowhere to go in --only mode: they bounce back
        into the group, which then plays to completion."""
        raw = cassette(group(ex("Squat", 10), rounds=2, rest=2))
        keys = [
            "b",                       # bounce off the transition
            "enter", "enter",          # transition + set 1
            "j",                       # 'j' during rest: bounces (menu absent)
            "enter", "enter",          # restarted rest + set 2
        ]
        c = self.play(raw, 0, 0, "Squat", keys)

        squat = c.phases[0].groups[0].exercises[0]
        assert [s.actual_reps for s in squat.sets] == [10, 10]
        assert rounds_completed(c.phases[0].groups[0]) == 2

    def test_skip_logs_partial_as_skipped(self, no_audio):
        raw = cassette(group(ex("Squat", 10)), title="Skip Day")
        c = self.play(raw, 0, 0, "Squat", ["s"])
        assert c.phases[0].groups[0].skipped
        log = state_mod.LOG_FILE.read_text()
        assert "(partial: Squat)" in log
        assert "(skipped)" in log
