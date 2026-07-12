"""The player state machine: an explicit playhead over the cassette.

`Player` replaces the old quadruply-nested playback loops. It holds the
cassette plus a playhead (`phase_idx, group_idx, round_idx, ex_idx`) and
reacts to typed `Event`s returned by the screens in `screens.py`.

Movement is non-destructive: `previous_group` ('b') just moves the playhead
back one group — progress is never cleared. On arrival a group resumes at
its first incomplete set. `advance_group` moves to the next incomplete group
in cassette order, skipping done/skipped ones.
"""

import time
from typing import Callable

from rich.console import Console
from rich.live import Live

from .audio import play_sound, sound_group_complete, sound_set_complete
from .cassette import all_groups, count_sets, rounds_completed
from .events import Event
from .models import Cassette, Group
from .screens import (
    context_screen,
    get_failure_reps,
    rep_set_screen,
    rest_timer,
    timed_hold,
    transition_screen,
)
from .state import clear_state, print_log, save_log, save_state
from .tts import say, speak


def clear_group_progress(group: Group) -> None:
    """Reset all completed sets in a group so it can be replayed.

    Kept for the future explicit 'redo' action — the only deliberately
    destructive operation. Normal navigation never clears progress."""
    group.skipped = False
    for ex in group.exercises:
        for s in ex.sets:
            s.actual_reps = None
            s.failure = False


def speak_round_complete(group: Group, round_idx: int) -> None:
    if round_idx < len(group.voice_round_complete):
        speak(group.voice_round_complete[round_idx])


def group_done(group: Group) -> bool:
    """A group is done when it was skipped or all its rounds are complete."""
    if group.skipped or not group.exercises:
        return True
    return rounds_completed(group) >= group.rounds


class Player:
    """Explicit-playhead state machine over a cassette.

    `now` and `read_key` are injectable (default: real clock/terminal) so the
    player can be driven headlessly in tests. `sleep` likewise.
    """

    def __init__(
        self,
        cassette: Cassette,
        cassette_path: str | None = None,
        *,
        now: Callable[[], float] = time.time,
        read_key: Callable[[], str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cassette = cassette
        self.cassette_path = cassette_path
        self.now = now
        self.read_key = read_key
        self.sleep = sleep

        # Playhead
        self.phase_idx = 0
        self.group_idx = 0
        self.round_idx = 0
        self.ex_idx = 0

        self.rep_set_durations: list[float] = []

        # Set when 'b' interrupts a rest period: (phase_idx, group_idx) of the
        # group whose rest was cut short. Re-entering that group mid-round
        # restarts the rest instead of dropping the user straight into the
        # next set.
        self._pending_rest: tuple[int, int] | None = None

    # -- playhead queries ---------------------------------------------------

    def current_group(self) -> Group:
        return self.cassette.phases[self.phase_idx].groups[self.group_idx]

    def is_complete(self) -> bool:
        """True when every group in the cassette is done or skipped."""
        return all(group_done(g) for _, _, g in all_groups(self.cassette))

    def _flat_pos(self) -> tuple[list[tuple[int, int, Group]], int]:
        groups = all_groups(self.cassette)
        for i, (pi, gi, _) in enumerate(groups):
            if (pi, gi) == (self.phase_idx, self.group_idx):
                return groups, i
        raise ValueError(f"playhead ({self.phase_idx},{self.group_idx}) not in cassette")

    # -- playhead moves -----------------------------------------------------

    def jump_to(self, phase_idx: int, group_idx: int) -> None:
        """Move the playhead to a group; resume at its first incomplete set."""
        self.phase_idx = phase_idx
        self.group_idx = group_idx
        self.round_idx = rounds_completed(self.current_group())
        self.ex_idx = 0

    def advance_set(self) -> None:
        """Advance past the current set slot; rolls into the next round after
        the last exercise of a round."""
        group = self.current_group()
        self.ex_idx += 1
        if self.ex_idx >= len(group.exercises):
            self.ex_idx = 0
            self.round_idx += 1

    def advance_group(self) -> bool:
        """Move to the next incomplete group in cassette order, skipping
        done/skipped ones (wrapping to earlier incomplete groups if needed).
        Returns False when nothing is left to play."""
        groups, pos = self._flat_pos()
        for pi, gi, group in groups[pos + 1:] + groups[:pos]:
            if not group_done(group):
                self.jump_to(pi, gi)
                return True
        return False

    def previous_group(self) -> bool:
        """Move the playhead back one group. Pure playhead move — no progress
        is cleared. Returns False when already at the first group."""
        groups, pos = self._flat_pos()
        if pos == 0:
            return False
        pi, gi, _ = groups[pos - 1]
        self.jump_to(pi, gi)
        return True

    # -- playback -----------------------------------------------------------

    def avg_rep_set(self) -> float:
        if self.rep_set_durations:
            return sum(self.rep_set_durations) / len(self.rep_set_durations)
        return 30.0

    def run(self, live: Live) -> None:
        """Play from the playhead until the cassette is complete."""
        if group_done(self.current_group()) and not self.advance_group():
            return

        via_back = False
        spoken_phase: int | None = None
        while True:
            if not via_back and self.phase_idx != spoken_phase:
                speak(self.cassette.phases[self.phase_idx].voice_intro)
            spoken_phase = self.phase_idx

            ev = self.play_group(live, via_back)
            via_back = False
            if ev is Event.BACK:
                if self.previous_group():
                    via_back = True
                continue
            if not self.advance_group():
                return

    def play_group(self, live: Live, via_back: bool = False) -> Event:
        """Play the group under the playhead from its first incomplete set.
        Returns DONE when the group finished or was skipped, BACK on 'b'."""
        group = self.current_group()
        pi, gi = self.phase_idx, self.group_idx
        self.round_idx = rounds_completed(group)
        self.ex_idx = 0

        if not group.exercises:
            return Event.DONE

        if via_back:
            # Landing on a skipped group via back makes it playable again
            # (progress itself is never touched).
            group.skipped = False
            if self.round_idx >= group.rounds:
                # 'b' onto a finished group: offer an explicit redo — the one
                # deliberately destructive action (recommendations §3).
                ev = transition_screen(
                    live, self.cassette, pi, gi, group, self.avg_rep_set(),
                    completed=True,
                    now=self.now, read_key=self.read_key, sleep=self.sleep,
                )
                if ev is Event.BACK:
                    return Event.BACK
                if ev is not Event.REDO:
                    return Event.DONE
                clear_group_progress(group)
                self.round_idx = 0
                speak(group.voice_intro)
        elif self.round_idx >= group.rounds:
            # Already complete on arrival: nothing to play, no fanfare.
            return Event.DONE
        elif self.round_idx == 0:
            ev = transition_screen(
                live, self.cassette, pi, gi, group, self.avg_rep_set(),
                now=self.now, read_key=self.read_key, sleep=self.sleep,
            )
            if ev is Event.BACK:
                return Event.BACK
            if ev is Event.SKIP:
                group.skipped = True
                return Event.DONE
            speak(group.voice_intro)
        # Note: the intro is only spoken at round 0 — re-entering a group
        # mid-round (e.g. after 'b' bounced off the first group) must not
        # repeat it.

        # If 'b' interrupted this group's rest period, restart the rest now
        # instead of dropping the user straight into the next set.
        if self._pending_rest == (pi, gi) and 0 < self.round_idx < group.rounds:
            self._pending_rest = None
            ev = rest_timer(
                live, self.cassette, pi, gi, group.rest, self.avg_rep_set(),
                now=self.now, read_key=self.read_key, sleep=self.sleep,
            )
            if ev is Event.SKIP:
                return self._skip_group(group, self.round_idx)
            if ev is Event.BACK:
                self._pending_rest = (pi, gi)
                return Event.BACK
        else:
            self._pending_rest = None

        while self.round_idx < group.rounds:
            r = self.round_idx
            ex = group.exercises[self.ex_idx]
            set_data = ex.sets[r]

            if set_data.actual_reps is None:
                if ex.timed:
                    ev = timed_hold(
                        live, self.cassette, pi, gi, group, ex, r, self.ex_idx,
                        self.avg_rep_set(),
                        now=self.now, read_key=self.read_key, sleep=self.sleep,
                    )
                    if ev is Event.SKIP:
                        return self._skip_group(group, r)
                    if ev is Event.BACK:
                        return Event.BACK
                    set_data.actual_reps = set_data.reps
                else:
                    set_start = self.now()
                    ev, paused_secs = rep_set_screen(
                        live, self.cassette, pi, gi, group, ex, r, self.ex_idx,
                        self.avg_rep_set(),
                        now=self.now, read_key=self.read_key, sleep=self.sleep,
                    )
                    if ev is Event.SKIP:
                        return self._skip_group(group, r)
                    if ev is Event.BACK:
                        return Event.BACK
                    if ev is Event.FAIL:
                        actual = get_failure_reps(
                            live, self.cassette, pi, gi, group, ex, r, self.ex_idx,
                            set_data.reps, self.avg_rep_set(),
                            now=self.now, read_key=self.read_key, sleep=self.sleep,
                        )
                        set_data.actual_reps = actual
                        set_data.failure = True
                    else:
                        set_data.actual_reps = set_data.reps
                    self.rep_set_durations.append(self.now() - set_start - paused_secs)

                # Set complete
                play_sound(sound_set_complete())

            last_in_round = self.ex_idx == len(group.exercises) - 1
            self.advance_set()

            if last_in_round:
                # Round complete
                speak_round_complete(group, r)
                play_sound(sound_set_complete())
                save_state(
                    self.cassette,
                    {"phase_idx": pi, "group_idx": gi, "round_idx": r + 1},
                    self.cassette_path,
                )

                # Rest (not after the group's final round)
                if r < group.rounds - 1:
                    ev = rest_timer(
                        live, self.cassette, pi, gi, group.rest, self.avg_rep_set(),
                        now=self.now, read_key=self.read_key, sleep=self.sleep,
                    )
                    if ev is Event.SKIP:
                        return self._skip_group(group, r + 1)
                    if ev is Event.BACK:
                        # Remember the interrupted rest so re-entering this
                        # group restarts it rather than skipping straight to
                        # the next set.
                        self._pending_rest = (pi, gi)
                        return Event.BACK

        # Group complete
        if not group.skipped:
            speak(group.voice_group_complete)
            play_sound(sound_group_complete())
        return Event.DONE

    def _skip_group(self, group: Group, round_idx: int) -> Event:
        group.skipped = True
        say(f"Skipping {group.exercises[0].name}")
        save_state(
            self.cassette,
            {"phase_idx": self.phase_idx, "group_idx": self.group_idx, "round_idx": round_idx},
            self.cassette_path,
        )
        return Event.DONE


# ---------------------------------------------------------------------------
# Top-level playback
# ---------------------------------------------------------------------------

def play_cassette(
    cassette: Cassette,
    cassette_path: str | None = None,
    *,
    now: Callable[[], float] = time.time,
    read_key: Callable[[], str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Play a cassette from start to finish (or from resume position)."""
    console = Console()

    # Check if already complete
    total, done = count_sets(cassette)
    if total > 0 and done >= total:
        console.print("[green]All exercises already complete![/green]")
        print_log(cassette)
        save_log(cassette)
        clear_state()  # otherwise every re-run would append another log entry
        return

    player = Player(cassette, cassette_path, now=now, read_key=read_key, sleep=sleep)

    speak(cassette.voice_session_intro)

    with Live(console=console, refresh_per_second=4, screen=True) as live:
        for ctx in cassette.context_exercises:
            speak(ctx.voice)
            context_screen(
                live, cassette, ctx.name, ctx.note, player.avg_rep_set(),
                now=now, read_key=read_key, sleep=sleep,
            )

        player.run(live)

    speak(cassette.voice_session_complete)
    console.print("[bold green]Workout complete![/bold green]\n")
    print_log(cassette)
    save_log(cassette)
    clear_state()
