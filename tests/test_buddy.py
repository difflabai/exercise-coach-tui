"""Tests for Rep, the ASCII buddy coach (recommendations §2, PR A).

Covers: buddy_panel frame cycling/rotation at explicit frame times (it is a
pure function — no clock needed), panel snapshot substrings via
Console(record=True)/export_text, narrow-width collapse, per-screen mood
selection (screens driven headlessly with a fake clock + scripted keys), the
transient celebration window stamped by the player, caption-to-speech-bubble
routing (and the classic caption row when the buddy is off or no mood is
passed), and --no-buddy/--buddy persistence via settings.json.
"""

import json
from io import StringIO

from rich.console import Console
from rich.live import Live
from rich.panel import Panel

from exercise_coach import audio, buddy, cli, screens, tts, ui
from exercise_coach import settings as user_settings
from exercise_coach.buddy import Mood, buddy_panel
from exercise_coach.cassette import load_cassette_from_dict
from exercise_coach.events import Event

# Distinctive art tokens per mood/frame set (all frames of a set contain them).
READY_FACE = "(•‿•)"
WORKING_FACE = "(ง"
HOLD_CALM_FACE = "(¬_¬"
HOLD_TREMBLE_FACE = "(>_<)"
REST_ASLEEP_FACE = "(－ω－)"
REST_AWAKE_FACE = "(o_o)"
OVERTIME_FACE = "(ಠ_ಠ)"
CELEBRATE_FACE = "\\(^o^)"
PAUSED_FACE = "(‑_‑)"


def render_to_text(renderable, width: int = 100) -> str:
    console = Console(file=StringIO(), record=True, width=width)
    console.print(renderable)
    return console.export_text()


class Clock:
    """Inline now/sleep pair; sleep just advances the fake clock."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.t = start

    def now(self) -> float:
        return self.t

    def sleep(self, secs: float) -> None:
        self.t += secs


def make_live(width: int = 100) -> Live:
    # Not started: live.update() just stores the frame for inspection.
    return Live(console=Console(file=StringIO(), force_terminal=True, width=width, height=50))


def last_frame(live: Live, width: int = 100) -> str:
    return render_to_text(live.renderable, width)


def scripted(*keys: str):
    queue = list(keys)
    return lambda: queue.pop(0) if queue else ""


# ---------------------------------------------------------------------------
# buddy_panel: frames, rotation, intensity, narrow collapse
# ---------------------------------------------------------------------------

class TestBuddyPanel:
    def test_snapshot_contains_title_art_and_speech(self):
        text = render_to_text(buddy_panel(Mood.READY, 0.0, "Next up: RDLs!"), width=40)
        assert "Rep" in text
        assert READY_FACE in text
        assert "“Next up: RDLs!”" in text

    def test_silent_buddy_falls_back_to_encouragement(self):
        text = render_to_text(buddy_panel(Mood.WORKING, 0.0))
        assert "“Make every rep count!”" in text

    def test_frames_cycle_and_wrap(self):
        f0 = render_to_text(buddy_panel(Mood.WORKING, 0.0))
        f1 = render_to_text(buddy_panel(Mood.WORKING, 0.5))
        assert f0 != f1  # 2 fps: half a second flips the frame
        assert render_to_text(buddy_panel(Mood.WORKING, 1.0)) == f0  # wraps around

    def test_output_is_deterministic(self):
        a = render_to_text(buddy_panel(Mood.RESTING, 123.4, intensity=30.0))
        b = render_to_text(buddy_panel(Mood.RESTING, 123.4, intensity=30.0))
        assert a == b

    def test_encouragement_rotates_over_time(self):
        # Frame index is identical at t=0 and t=8 (int(t*2) both even) —
        # only the rotated encouragement line differs.
        f0 = render_to_text(buddy_panel(Mood.PAUSED, 0.0))
        f8 = render_to_text(buddy_panel(Mood.PAUSED, 8.0))
        assert f0 != f8
        assert "“Paused. I'll wait.”" in f0
        assert "“Take your time.”" in f8

    def test_holding_trembles_near_the_end(self):
        calm = render_to_text(buddy_panel(Mood.HOLDING, 0.0, intensity=0.2))
        tense = render_to_text(buddy_panel(Mood.HOLDING, 0.0, intensity=0.95))
        assert HOLD_CALM_FACE in calm
        assert HOLD_TREMBLE_FACE not in calm
        assert HOLD_TREMBLE_FACE in tense

    def test_resting_wakes_up_with_ten_seconds_left(self):
        asleep = render_to_text(buddy_panel(Mood.RESTING, 0.0, intensity=45.0))
        awake = render_to_text(buddy_panel(Mood.RESTING, 0.0, intensity=8.0))
        assert REST_ASLEEP_FACE in asleep
        assert "zzz" in asleep.lower()
        assert REST_AWAKE_FACE in awake
        assert "zz" not in awake.lower()

    def test_overtime_taps_faster_per_nag(self):
        # At the base 2 fps a quarter second stays on frame 0; at nag level 4
        # (2 + 4 = 6 fps) it has already flipped.
        base = render_to_text(buddy_panel(Mood.OVERTIME, 0.0, intensity=0.0))
        assert render_to_text(buddy_panel(Mood.OVERTIME, 0.25, intensity=0.0)) == base
        assert render_to_text(buddy_panel(Mood.OVERTIME, 0.25, intensity=4.0)) != base

    def test_narrow_width_collapses_to_title_face(self):
        text = render_to_text(
            buddy_panel(Mood.RESTING, 0.0, "Shake it out.", intensity=60.0, width=50),
            width=50,
        )
        assert REST_ASLEEP_FACE in text          # face moved into the title
        assert "“Shake it out.”" in text         # body keeps only the speech line
        assert "_/" not in text                  # no art body
        assert len(text.strip().splitlines()) <= 4

    def test_wide_width_keeps_the_art(self):
        text = render_to_text(buddy_panel(Mood.WORKING, 0.0, width=80))
        assert WORKING_FACE in text


# ---------------------------------------------------------------------------
# Mood selection per screen (driven headlessly; last rendered frame inspected)
# ---------------------------------------------------------------------------

class TestScreenMoods:
    """Each screen routes its mood into render_layout; the buddy's face in
    the captured last frame identifies the mood that was rendered."""

    def _straight(self, straight_cassette, **kw):
        return load_cassette_from_dict(straight_cassette(**kw))

    def test_transition_screen_is_ready(self, straight_cassette, no_audio):
        cassette = self._straight(straight_cassette)
        live, clock = make_live(), Clock()
        screens.transition_screen(
            live, cassette, 0, 0, cassette.phases[0].groups[0],
            now=clock.now, read_key=scripted("enter"), sleep=clock.sleep,
        )
        assert READY_FACE in last_frame(live)

    def test_context_screen_is_ready(self, straight_cassette, no_audio):
        cassette = self._straight(straight_cassette)
        live, clock = make_live(), Clock()
        screens.context_screen(
            live, cassette, "Dead Hangs", "Accumulate 2 minutes.",
            now=clock.now, read_key=scripted("enter"), sleep=clock.sleep,
        )
        assert READY_FACE in last_frame(live)

    def test_rep_set_screen_is_working(self, straight_cassette, no_audio):
        cassette = self._straight(straight_cassette)
        group = cassette.phases[0].groups[0]
        live, clock = make_live(), Clock()
        screens.rep_set_screen(
            live, cassette, 0, 0, group, group.exercises[0], 0, 0,
            now=clock.now, read_key=scripted("enter"), sleep=clock.sleep,
        )
        assert WORKING_FACE in last_frame(live)

    def test_rest_timer_dozes_far_from_the_end(self, straight_cassette, no_audio):
        cassette = self._straight(straight_cassette)
        live, clock = make_live(), Clock()
        screens.rest_timer(
            live, cassette, 0, 0, 60,
            now=clock.now, read_key=scripted("enter"), sleep=clock.sleep,
        )
        assert REST_ASLEEP_FACE in last_frame(live)

    def test_rest_timer_wakes_buddy_with_ten_seconds_left(self, straight_cassette, no_audio):
        cassette = self._straight(straight_cassette)
        live, clock = make_live(), Clock()
        screens.rest_timer(
            live, cassette, 0, 0, 8,
            now=clock.now, read_key=scripted("enter"), sleep=clock.sleep,
        )
        assert REST_AWAKE_FACE in last_frame(live)

    def test_rest_overtime_is_overtime(self, straight_cassette, no_audio):
        cassette = self._straight(straight_cassette)
        live, clock = make_live(), Clock()
        # 1s rest, then ~20s of empty reads -> deep overtime before Enter.
        keys = [""] * 80 + ["enter"]
        screens.rest_timer(
            live, cassette, 0, 0, 1,
            now=clock.now, read_key=lambda: keys.pop(0), sleep=clock.sleep,
        )
        assert OVERTIME_FACE in last_frame(live)

    def test_timed_hold_countdown_is_ready(self, timed_cassette, no_audio):
        cassette = load_cassette_from_dict(timed_cassette(rounds=1, seconds=3, cues=[[]]))
        group = cassette.phases[0].groups[0]
        live, clock = make_live(), Clock()
        ev = screens.timed_hold(
            live, cassette, 0, 0, group, group.exercises[0], 0, 0,
            now=clock.now, read_key=scripted("s"), sleep=clock.sleep,
        )
        assert ev is Event.SKIP  # skipped during the countdown
        assert READY_FACE in last_frame(live)

    def test_timed_hold_trembles_near_the_end(self, timed_cassette, no_audio):
        cassette = load_cassette_from_dict(timed_cassette(rounds=1, seconds=3, cues=[[]]))
        group = cassette.phases[0].groups[0]
        live, clock = make_live(), Clock()
        screens.timed_hold(
            live, cassette, 0, 0, group, group.exercises[0], 0, 0,
            now=clock.now, read_key=scripted(), sleep=clock.sleep,
        )
        # The last rendered frame is moments before the hold completes:
        # elapsed fraction near 1.0 -> the trembling frame set.
        assert HOLD_TREMBLE_FACE in last_frame(live)

    def test_rest_overtime_intensity_follows_the_nag_count(
        self, straight_cassette, no_audio, monkeypatch,
    ):
        """The screen must wire state["nags"] into buddy_panel's intensity
        (the spec'd foot-tap speed-up per nag), not a constant."""
        seen: list[tuple[Mood, float]] = []
        real_buddy_panel = ui.buddy_panel

        def spy(mood, frame_time, speech="", *, intensity=0.0, width=80):
            seen.append((mood, intensity))
            return real_buddy_panel(
                mood, frame_time, speech, intensity=intensity, width=width,
            )

        monkeypatch.setattr(ui, "buddy_panel", spy)
        cassette = self._straight(straight_cassette)
        live, clock = make_live(), Clock()
        # 1s rest, then ~20s of empty reads -> past the first 15s nag.
        keys = [""] * 80 + ["enter"]
        screens.rest_timer(
            live, cassette, 0, 0, 1,
            now=clock.now, read_key=lambda: keys.pop(0), sleep=clock.sleep,
        )
        overtime = [i for m, i in seen if m is Mood.OVERTIME]
        assert overtime
        assert overtime[0] == 0.0   # overtime just started: no nag yet
        assert overtime[-1] == 1.0  # ~19s in: one nag -> faster foot-taps

    def test_pause_screen_is_paused(self, straight_cassette, no_audio):
        cassette = self._straight(straight_cassette)
        live, clock = make_live(), Clock()
        screens.pause_screen(
            live, cassette, 0, 0,
            now=clock.now, read_key=scripted("enter"), sleep=clock.sleep,
        )
        assert PAUSED_FACE in last_frame(live)


# ---------------------------------------------------------------------------
# Celebration window
# ---------------------------------------------------------------------------

class TestCelebration:
    def test_window_is_transient(self):
        buddy.celebrate(1000.0)
        assert buddy.celebrating(1000.0)
        assert buddy.celebrating(1001.9)
        assert not buddy.celebrating(1002.0)

    def test_reset_clears_the_window(self):
        buddy.celebrate(1000.0)
        buddy.reset()
        assert not buddy.celebrating(1000.0)

    def test_player_stamps_celebration_at_set_complete(self, make_player, straight_cassette):
        harness = make_player(straight_cassette(rounds=1), keys=["enter", "enter"])
        harness.run()
        assert harness.player.is_complete()
        assert buddy.celebrating(harness.clock.now())

    def test_failed_set_does_not_celebrate(self, make_player, straight_cassette):
        # 'f' + reps + Enter reports a failed set; 'b' interrupts the rest at
        # the exact moment the (skipped) celebration window would be open.
        harness = make_player(
            straight_cassette(rounds=2), keys=["enter", "f", "5", "enter", "b"],
        )
        ev = harness.play_group()
        assert ev is Event.BACK
        set0 = harness.cassette.phases[0].groups[0].exercises[0].sets[0]
        assert set0.failure is True and set0.actual_reps == 5
        assert not buddy.celebrating(harness.clock.now())

    def test_render_layout_overrides_mood_while_celebrating(self, no_audio):
        buddy.celebrate(1_000_000.0)
        live = make_live()
        ui.render_layout(live, Panel("overview"), Panel("active"), "progress",
                         mood=Mood.WORKING, now=1_000_000.5)
        assert CELEBRATE_FACE in last_frame(live)

    def test_override_expires_back_to_the_screen_mood(self, no_audio):
        buddy.celebrate(1_000_000.0)
        live = make_live()
        ui.render_layout(live, Panel("overview"), Panel("active"), "progress",
                         mood=Mood.WORKING, now=1_000_002.5)
        text = last_frame(live)
        assert CELEBRATE_FACE not in text
        assert WORKING_FACE in text


# ---------------------------------------------------------------------------
# Caption routing: speech bubble vs the classic caption row
# ---------------------------------------------------------------------------

class TestCaptionRouting:
    def test_caption_renders_in_the_bubble_not_the_caption_row(self, no_audio):
        tts.say("Next up: squats")
        live = make_live()
        ui.render_layout(live, Panel("overview"), Panel("active"), "progress",
                         mood=Mood.WORKING, now=1_000_000.0)
        text = last_frame(live)
        assert "“Next up: squats”" in text
        assert "🗣" not in text  # no separate caption row in the buddy layout

    def test_expired_caption_falls_back_to_encouragement(self, no_audio, monkeypatch):
        tts.say("Next up: squats")
        # Age the caption past its display window: the bubble must drop it
        # and fall back to the rotating encouragement pool, not stick forever.
        monkeypatch.setattr(
            tts, "_caption_time", tts._caption_time - (tts.CAPTION_DURATION + 1.0),
        )
        live = make_live()
        ui.render_layout(live, Panel("overview"), Panel("active"), "progress",
                         mood=Mood.WORKING, now=1_000_000.0)
        text = last_frame(live)
        assert "Next up: squats" not in text
        assert "“Make every rep count!”" in text  # pool index 0 at this frame_time

    def test_disabled_buddy_keeps_the_caption_row(self, no_audio):
        user_settings.buddy_enabled = False
        tts.say("Next up: squats")
        live = make_live()
        ui.render_layout(live, Panel("overview"), Panel("active"), "progress",
                         mood=Mood.WORKING, now=1_000_000.0)
        text = last_frame(live)
        assert "🗣" in text
        assert "Next up: squats" in text
        assert WORKING_FACE not in text
        assert "Rep" not in text

    def test_no_mood_renders_the_classic_layout(self, no_audio):
        tts.say("hello")
        live = make_live()
        ui.render_layout(live, Panel("overview"), Panel("active"), "progress")
        text = last_frame(live)
        assert "🗣" in text
        assert "hello" in text
        assert "Rep" not in text


# ---------------------------------------------------------------------------
# Narrow-terminal layout: the buddy must never starve the active panel
# ---------------------------------------------------------------------------

class TestNarrowLayout:
    """Below COLLAPSE_WIDTH columns render_layout must not reserve a side
    column: the active panel keeps the full terminal width and the collapsed
    one-line buddy stacks underneath. Uses the real console width from the
    Live, so these also pin the width wiring into buddy_panel."""

    def test_narrow_console_keeps_the_active_panel_full_width(self, no_audio):
        live = make_live(width=50)
        ui.render_layout(live, Panel("overview"), Panel("active panel content here"),
                         "progress", mood=Mood.WORKING, now=0.0)
        text = last_frame(live, width=50)
        assert "active panel content here" in text  # one line — no side-column wrap
        assert "Rep (ง'-')ง" in text                # collapsed face in the title
        assert "o==" not in text                    # no 5-row dumbbell art body

    def test_very_narrow_console_still_renders_the_active_panel(self, no_audio):
        # At 30 columns the old fixed 30-col side column consumed the entire
        # width and the active panel was not rendered at all.
        live = make_live(width=30)
        ui.render_layout(live, Panel("overview"), Panel("Timer: 45s HOLD!"),
                         "progress", mood=Mood.HOLDING, mood_intensity=0.2, now=0.0)
        text = last_frame(live, width=30)
        assert "Timer: 45s HOLD!" in text
        assert "Rep (¬_¬)" in text

    def test_wide_console_keeps_the_side_by_side_art(self, no_audio):
        live = make_live(width=100)
        ui.render_layout(live, Panel("overview"), Panel("active"), "progress",
                         mood=Mood.WORKING, now=0.0)
        text = last_frame(live)
        assert "o==|==o" in text  # full art body in the 30-col side column


# ---------------------------------------------------------------------------
# --no-buddy / --buddy persistence
# ---------------------------------------------------------------------------

class TestBuddyPersistence:
    def test_no_buddy_flag_persists_without_clobbering_volume(
        self, isolated_state, monkeypatch, capsys,
    ):
        audio.settings.volume = 0.9
        audio.save_settings()
        # --reset exits early, right after the settings are applied.
        monkeypatch.setattr("sys.argv", ["coach", "--no-buddy", "--reset"])
        cli.main()
        assert user_settings.buddy_enabled is False
        data = json.loads((isolated_state / "settings.json").read_text())
        assert data["buddy"] is False
        assert data["volume"] == 0.9  # merge-write: the saved volume survives

    def test_next_load_sees_the_persisted_choice(self, isolated_state, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", ["coach", "--no-buddy", "--reset"])
        cli.main()
        user_settings.buddy_enabled = True  # a fresh process starts at the default
        user_settings.load_buddy_enabled()
        assert user_settings.buddy_enabled is False

    def test_buddy_flag_reenables_and_persists(self, isolated_state, monkeypatch, capsys):
        user_settings.set_buddy_enabled(False)
        monkeypatch.setattr("sys.argv", ["coach", "--buddy", "--reset"])
        cli.main()
        assert user_settings.buddy_enabled is True
        data = json.loads((isolated_state / "settings.json").read_text())
        assert data["buddy"] is True

    def test_no_flag_keeps_default_on_and_writes_nothing(
        self, isolated_state, monkeypatch, capsys,
    ):
        monkeypatch.setattr("sys.argv", ["coach", "--reset"])
        cli.main()
        assert user_settings.buddy_enabled is True
        assert not (isolated_state / "settings.json").exists()

    def test_load_with_missing_key_keeps_default(self, isolated_state):
        user_settings.load_buddy_enabled()
        assert user_settings.buddy_enabled is True
