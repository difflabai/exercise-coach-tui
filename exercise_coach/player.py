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
from statistics import median
from typing import Callable

from rich.console import Console
from rich.live import Live

from . import settings as user_settings
from . import ui
from .audio import play_sound, sound_group_complete, sound_set_complete
from .buddy import celebrate
from .cassette import all_groups, count_sets, rounds_completed
from .events import Event
from .history import append_record, build_record, last_session_for_title
from .jumpmenu import JumpTarget
from .models import Cassette, Group
from .screens import (
    context_screen,
    get_failure_reps,
    jump_menu_screen,
    rep_set_screen,
    rest_timer,
    timed_hold,
    transition_screen,
)
from .state import (
    clear_state,
    print_log,
    render_partial_log,
    save_log,
    save_partial_log,
    save_state,
)
from .term import WorkoutPaused, restore_terminal
from .tts import say, speak, terminate_say


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
        persist_state: bool = True,
    ) -> None:
        self.cassette = cassette
        self.cassette_path = cassette_path
        self.now = now
        self.read_key = read_key
        self.sleep = sleep
        # --only runs disable this so they can never clobber a saved
        # full-session state file.
        self.persist_state = persist_state

        # Playhead
        self.phase_idx = 0
        self.group_idx = 0
        self.round_idx = 0
        self.ex_idx = 0

        self.rep_set_durations: list[float] = []
        # Per-exercise rep-set durations, for the persisted pace medians
        # (timed holds already know their duration and are never tracked).
        self.set_durations_by_name: dict[str, list[float]] = {}

        # (phase_idx, group_idx) of every group whose rest period was cut
        # short by 'b' (or 'j'). Re-entering such a group mid-round restarts
        # its rest instead of dropping the user straight into the next set.
        # A set, not a single slot: jumping around can interrupt several
        # groups' rests and each one stays owed independently.
        self._pending_rest: set[tuple[int, int]] = set()
        # Every screen's ETA must charge the rests still owed here, but only
        # this player knows them — publish the live set the same way the
        # learned paces reach build_progress_bar (ui reads the module global).
        ui.pending_rests = self._pending_rest

        # How to re-enter the interrupted group when the jump menu is
        # cancelled: True = the via_back way (no transition screen, no intro
        # replay — set by rest/rep-set/completed screens), False = re-show
        # the setup screen (set by the fresh transition screen, where
        # nothing has started yet).
        self._jump_resume_via_back = True

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

    def _record_rep_duration(self, name: str, secs: float) -> None:
        self.rep_set_durations.append(secs)
        self.set_durations_by_name.setdefault(name, []).append(secs)

    def session_paces(self) -> dict[str, int]:
        """Median observed rep-set seconds per exercise name this session —
        what update_paces persists at session end so the NEXT session's ETA
        is right from set one."""
        return {
            name: round(median(durs))
            for name, durs in self.set_durations_by_name.items()
        }

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
            if ev is Event.JUMP:
                via_back = self._apply_jump(live)
                continue
            if not self.advance_group():
                return

    def _apply_jump(self, live: Live) -> bool:
        """Show the jump menu and apply the selection.

        Returns True when the interrupted group should be re-entered the
        via_back way (menu cancelled from a mid-group screen: no transition
        replay, no re-spoken intro, completed sets stay guarded)."""
        target = jump_menu_screen(
            live, self.cassette, self.phase_idx, self.group_idx,
            self.avg_rep_set(),
            now=self.now, read_key=self.read_key, sleep=self.sleep,
        )
        if target is None:
            return self._jump_resume_via_back
        group = self.cassette.phases[target.phase_idx].groups[target.group_idx]
        if target.redo:
            # Reuses the REDO semantics: the one destructive action, already
            # 'y'-confirmed inside the menu. Only this group is cleared.
            clear_group_progress(group)
            self.jump_to(target.phase_idx, target.group_idx)
            return False
        if target.round_idx is not None:
            self.play_single_set(live, target)
            # One set only: do NOT continue the rest of that group — return
            # to the normal flow at the next incomplete group in cassette
            # order. When nothing else is incomplete this is a no-op and
            # run() resumes this same group at its first incomplete set.
            self.advance_group()
            return False
        # Group-level jump: never clears progress. A skipped group becomes
        # playable again, and play resumes at its first incomplete set
        # (jump_to's rounds_completed arithmetic).
        group.skipped = False
        self.jump_to(target.phase_idx, target.group_idx)
        return False

    def play_single_set(self, live: Live, target: JumpTarget) -> None:
        """Play exactly one exercise's set slot (a set-level jump).

        For a superset this is one exercise of the round, not the whole
        round — the spec-free way to do one side of a unilateral exercise.
        The result is recorded exactly like normal play (actual_reps /
        failure, state saved); the caller then returns to the normal flow
        instead of continuing the group. SKIP/BACK/JUMP mid-set abandon the
        detour without recording anything (and without skip-flagging the
        group)."""
        pi, gi = target.phase_idx, target.group_idx
        group = self.cassette.phases[pi].groups[gi]
        ex = group.exercises[target.ex_idx]
        set_data = ex.sets[target.round_idx]
        self.phase_idx, self.group_idx = pi, gi
        self.round_idx, self.ex_idx = target.round_idx, target.ex_idx
        r = target.round_idx

        if ex.timed:
            ev, held = timed_hold(
                live, self.cassette, pi, gi, group, ex, r, target.ex_idx,
                self.avg_rep_set(),
                now=self.now, read_key=self.read_key, sleep=self.sleep,
            )
            if ev in (Event.SKIP, Event.BACK):
                return
            if ev is Event.FAIL:
                # "I broke earlier": prompt for the actual seconds held.
                actual = get_failure_reps(
                    live, self.cassette, pi, gi, group, ex, r, target.ex_idx,
                    set_data.reps, self.avg_rep_set(),
                    now=self.now, read_key=self.read_key, sleep=self.sleep,
                )
                set_data.actual_reps = actual
                set_data.failure = True
            else:
                # Ended early via Enter (a shortfall is a failure) or ran out.
                set_data.actual_reps = held
                set_data.failure = held < set_data.reps
        else:
            set_start = self.now()
            ev, paused_secs = rep_set_screen(
                live, self.cassette, pi, gi, group, ex, r, target.ex_idx,
                self.avg_rep_set(),
                now=self.now, read_key=self.read_key, sleep=self.sleep,
            )
            # Measure the set the moment its screen ends: the failure-reps
            # prompt below is thinking-and-typing time, not set-work time,
            # and must never pollute the learned pace.
            set_secs = self.now() - set_start - paused_secs
            if ev in (Event.SKIP, Event.BACK, Event.JUMP):
                return
            if ev is Event.FAIL:
                actual = get_failure_reps(
                    live, self.cassette, pi, gi, group, ex, r, target.ex_idx,
                    set_data.reps, self.avg_rep_set(),
                    now=self.now, read_key=self.read_key, sleep=self.sleep,
                )
                set_data.actual_reps = actual
                set_data.failure = True
            else:
                set_data.actual_reps = set_data.reps
            self._record_rep_duration(ex.name, set_secs)

        # Only now that a set was actually performed does the detour revive a
        # skipped group — an abandoned detour (the early returns above) must
        # leave an explicit skip untouched.
        group.skipped = False
        play_sound(sound_set_complete())
        if not set_data.failure:
            celebrate(self.now())
        if group_done(group):
            # This set filled the group's last missing slot: give it the same
            # group-complete fanfare as normal play (the subsequent play_group
            # re-entry sees it already complete and deliberately stays silent).
            speak(group.voice_group_complete)
            play_sound(sound_group_complete())
            celebrate(self.now())
        self._save_state(
            {"phase_idx": pi, "group_idx": gi, "round_idx": rounds_completed(group)},
        )

    def play_group(self, live: Live, via_back: bool = False) -> Event:
        """Play the group under the playhead from its first incomplete set.
        Returns DONE when the group finished or was skipped, BACK on 'b',
        JUMP on 'j' (the in-flight set is not recorded in either case)."""
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
                if ev is Event.JUMP:
                    self._jump_resume_via_back = True  # cancel re-shows this screen
                    return Event.JUMP
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
            if ev is Event.JUMP:
                # Nothing has started yet: a cancelled menu should land back
                # on this setup screen, not mid-group.
                self._jump_resume_via_back = False
                return Event.JUMP
            if ev is Event.SKIP:
                group.skipped = True
                return Event.DONE
            speak(group.voice_intro)
        # Note: the intro is only spoken at round 0 — re-entering a group
        # mid-round (e.g. after 'b' bounced off the first group) must not
        # repeat it.

        # If 'b' interrupted this group's rest period, restart the rest now
        # instead of dropping the user straight into the next set.
        if (pi, gi) in self._pending_rest and 0 < self.round_idx < group.rounds:
            self._pending_rest.discard((pi, gi))
            ev = rest_timer(
                live, self.cassette, pi, gi, group.rest, self.avg_rep_set(),
                now=self.now, read_key=self.read_key, sleep=self.sleep,
            )
            if ev is Event.SKIP:
                return self._skip_group(group, self.round_idx)
            if ev in (Event.BACK, Event.JUMP):
                # Jumping away during a rest behaves exactly like BACK: the
                # rest stays pending, so returning to this group mid-round
                # restarts it rather than dropping straight into the next set.
                self._pending_rest.add((pi, gi))
                self._jump_resume_via_back = True
                return ev
        elif (pi, gi) in self._pending_rest:
            # A pending rest for THIS group that no longer applies (round 0
            # after a redo, or the group completed elsewhere): drop it. A
            # pending rest for a DIFFERENT group deliberately survives playing
            # this one — jumping away mid-rest and doing other work doesn't
            # waive the rest still owed before that group's next set.
            self._pending_rest.discard((pi, gi))

        played_in_round = False
        while self.round_idx < group.rounds:
            r = self.round_idx
            ex = group.exercises[self.ex_idx]
            set_data = ex.sets[r]

            if set_data.actual_reps is None:
                played_in_round = True
                if ex.timed:
                    ev, held = timed_hold(
                        live, self.cassette, pi, gi, group, ex, r, self.ex_idx,
                        self.avg_rep_set(),
                        now=self.now, read_key=self.read_key, sleep=self.sleep,
                    )
                    if ev is Event.SKIP:
                        return self._skip_group(group, r)
                    if ev is Event.BACK:
                        return Event.BACK
                    if ev is Event.FAIL:
                        # "I broke earlier": prompt for the actual seconds held.
                        actual = get_failure_reps(
                            live, self.cassette, pi, gi, group, ex, r, self.ex_idx,
                            set_data.reps, self.avg_rep_set(),
                            now=self.now, read_key=self.read_key, sleep=self.sleep,
                        )
                        set_data.actual_reps = actual
                        set_data.failure = True
                    else:
                        # Ended early via Enter (a shortfall is a failure) or
                        # the timer ran out (a full hold).
                        set_data.actual_reps = held
                        set_data.failure = held < set_data.reps
                else:
                    set_start = self.now()
                    ev, paused_secs = rep_set_screen(
                        live, self.cassette, pi, gi, group, ex, r, self.ex_idx,
                        self.avg_rep_set(),
                        now=self.now, read_key=self.read_key, sleep=self.sleep,
                    )
                    # Measured before the failure-reps prompt can run: time
                    # spent typing a missed-rep count is not set-work time
                    # and must never pollute the learned pace.
                    set_secs = self.now() - set_start - paused_secs
                    if ev is Event.SKIP:
                        return self._skip_group(group, r)
                    if ev is Event.BACK:
                        return Event.BACK
                    if ev is Event.JUMP:
                        # The in-flight set simply isn't recorded, same as BACK.
                        self._jump_resume_via_back = True
                        return Event.JUMP
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
                    self._record_rep_duration(ex.name, set_secs)

                # Set complete. No celebration after a reported failure —
                # Rep cheering "Nailed it!" right after the user typed a
                # missed rep count would read as mockery.
                play_sound(sound_set_complete())
                if not set_data.failure:
                    celebrate(self.now())

            last_in_round = self.ex_idx == len(group.exercises) - 1
            self.advance_set()

            if last_in_round and played_in_round:
                # Round complete (same failure gate: for straight sets this
                # fires in the same instant as the set-complete stamp above,
                # so it must not reopen the celebration window either).
                # A round whose sets were all recorded earlier (set-level
                # jumps) is passed over silently: no announcement, no chime,
                # no save, and no second rest with no work in between.
                speak_round_complete(group, r)
                play_sound(sound_set_complete())
                if not set_data.failure:
                    celebrate(self.now())
                self._save_state({"phase_idx": pi, "group_idx": gi, "round_idx": r + 1})

                # Rest (not after the group's final round)
                if r < group.rounds - 1:
                    ev = rest_timer(
                        live, self.cassette, pi, gi, group.rest, self.avg_rep_set(),
                        now=self.now, read_key=self.read_key, sleep=self.sleep,
                    )
                    if ev is Event.SKIP:
                        return self._skip_group(group, r + 1)
                    if ev in (Event.BACK, Event.JUMP):
                        # Remember the interrupted rest so re-entering this
                        # group restarts it rather than skipping straight to
                        # the next set (BACK and JUMP alike).
                        self._pending_rest.add((pi, gi))
                        self._jump_resume_via_back = True
                        return ev
            if last_in_round:
                played_in_round = False

        # Group complete
        if not group.skipped:
            speak(group.voice_group_complete)
            play_sound(sound_group_complete())
            celebrate(self.now())
        return Event.DONE

    def _skip_group(self, group: Group, round_idx: int) -> Event:
        group.skipped = True
        say(f"Skipping {group.exercises[0].name}")
        self._save_state(
            {"phase_idx": self.phase_idx, "group_idx": self.group_idx, "round_idx": round_idx},
        )
        return Event.DONE

    def _save_state(self, position: dict) -> None:
        if self.persist_state:
            save_state(self.cassette, position, self.cassette_path)


# ---------------------------------------------------------------------------
# Top-level playback
# ---------------------------------------------------------------------------

def _persist_paces(player: Player) -> None:
    """Persist this session's learned per-exercise paces (at save_log time)."""
    paces = player.session_paces()
    if paces:
        user_settings.update_paces(paces)


def play_cassette(
    cassette: Cassette,
    cassette_path: str | None = None,
    *,
    now: Callable[[], float] = time.time,
    read_key: Callable[[], str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    prior_elapsed: float = 0.0,
) -> None:
    """Play a cassette from start to finish (or from resume position).

    `prior_elapsed` is active seconds already spent on this session before
    this invocation — cli.main passes it when re-entering after a Ctrl-Z
    suspend, and from the saved state's session_elapsed on a Ctrl-C resume —
    so the history record's duration covers the whole session, not just the
    final segment."""
    console = Console()
    # Session wall clock, on the player's (injectable) clock, backdated by
    # the time already worked before this (re-)invocation.
    start = now() - prior_elapsed

    # Check if already complete
    total, done = count_sets(cassette)
    if total > 0 and done >= total:
        # No history record here: no workout happened in this run (the run
        # that produced the progress already appended its own record), and a
        # 0-duration full record would poison the next session's comparison.
        console.print("[green]All exercises already complete![/green]")
        print_log(cassette)
        save_log(cassette)
        clear_state()  # otherwise every re-run would append another log entry
        return

    player = Player(cassette, cassette_path, now=now, read_key=read_key, sleep=sleep)

    speak(cassette.voice_session_intro)

    try:
        with Live(console=console, refresh_per_second=4, screen=True) as live:
            for ctx in cassette.context_exercises:
                speak(ctx.voice)
                context_screen(
                    live, cassette, ctx.name, ctx.note, player.avg_rep_set(),
                    now=now, read_key=read_key, sleep=sleep,
                )

            player.run(live)
    except KeyboardInterrupt:
        # The prose log entry is written by the CLI's Ctrl-C handler; the
        # structured history record needs the player's clock and paces, so
        # it is appended here on the way out. partial=True: an aborted
        # session must never become last_session_for_title's comparison
        # baseline (and a later resumed completion appends the real record).
        append_record(build_record(
            cassette, now() - start, player.session_paces(), partial=True,
        ))
        raise

    duration = now() - start
    record = build_record(cassette, duration, player.session_paces())
    # Look up the previous session BEFORE appending this one.
    previous = last_session_for_title(record["title"])

    speak(cassette.voice_session_complete)
    console.print("[bold green]Workout complete![/bold green]\n")
    print_log(cassette)
    save_log(cassette)
    append_record(record)
    console.print(ui.build_summary_table(record, previous))
    console.print()
    _persist_paces(player)
    clear_state()


def play_only_group(
    cassette: Cassette,
    phase_idx: int,
    group_idx: int,
    label: str,
    *,
    now: Callable[[], float] = time.time,
    read_key: Callable[[], str] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Play a single group start-to-finish (--only), log a partial session.

    Design choices (recommendations §3):
    - State persistence is OFF (``persist_state=False``): an --only run must
      never clobber a saved full-session state — the state file is left
      byte-identical.
    - The jump menu is effectively absent: there is nowhere else to go, so
      'j' (like 'b') just bounces back into the group; mid-group re-entry
      resumes at the first incomplete set and an interrupted rest restarts
      via the pending-rest machinery.
    - Ctrl-C / Ctrl-Z end the run (nothing to resume without state); the
      partial log entry is still written.
    """
    console = Console()
    start = now()
    player = Player(
        cassette, None, now=now, read_key=read_key, sleep=sleep,
        persist_state=False,
    )
    player.jump_to(phase_idx, group_idx)
    group = player.current_group()
    try:
        with Live(console=console, refresh_per_second=4, screen=True) as live:
            while not group_done(group):
                player.play_group(live)
    except (KeyboardInterrupt, WorkoutPaused):
        restore_terminal()
        terminate_say()
        console.print("\n[yellow]Stopped.[/yellow]")
    print("\n" + render_partial_log(cassette, phase_idx, group_idx) + "\n")
    save_partial_log(cassette, phase_idx, group_idx, label)
    append_record(build_record(
        cassette, now() - start, player.session_paces(),
        partial=True, matched_name=label, group_pos=(phase_idx, group_idx),
    ))
    _persist_paces(player)
