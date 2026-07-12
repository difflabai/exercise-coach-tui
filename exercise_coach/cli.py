"""CLI entry point: argparse, resume flow, top-level run loop."""

import argparse
import os
import signal
import sys
import time
from pathlib import Path

from rich.console import Console

from . import settings as user_settings
from .audio import load_settings
from .audio import settings as audio_settings
from .cassette import (
    cassette_content_hash,
    count_sets,
    parse_input,
    read_input,
    rounds_completed,
)
from .jumpmenu import build_jump_rows, match_only
from .models import Cassette
from .player import play_cassette, play_only_group
from .state import (
    apply_state,
    clear_state,
    load_state_data,
    migrate_legacy_files,
    print_log,
    render_log,
    save_log,
    save_state,
)
from .term import WorkoutPaused, restore_terminal
from .tts import terminate_say

STALE_HOURS = 4


# ---------------------------------------------------------------------------
# Resume logic
# ---------------------------------------------------------------------------

def try_resume(cassette: Cassette, cassette_path: str | None, auto: bool = False) -> dict | None:
    """Check for saved state and optionally resume. Returns resume position or None."""
    console = Console(stderr=True)
    state = load_state_data()
    if state is None:
        return None

    ts = state.get("timestamp", 0)
    age_hours = (time.time() - ts) / 3600

    # Verify cassette hash if we have a file path
    if cassette_path:
        saved_hash = state.get("cassette_hash", "")
        current_hash = cassette_content_hash(cassette_path)
        if saved_hash and saved_hash != current_hash:
            console.print("[yellow]Saved state doesn't match this cassette, starting fresh.[/yellow]")
            clear_state()
            return None

    # Check progress
    groups_state = state.get("groups_state", [])
    total_done = sum(
        sum(1 for ex_sets in gs.get("sets", []) for s in ex_sets if s.get("actual_reps") is not None)
        for gs in groups_state
    )
    total_sets, _ = count_sets(cassette)

    # Don't resume a completed workout
    if total_done >= total_sets:
        clear_state()
        return None

    if age_hours > STALE_HOURS:
        console.print(f"[yellow]Found saved state from {age_hours:.1f} hours ago.[/yellow]")

    console.print(f"[cyan]Saved progress found: {total_done}/{total_sets} sets complete.[/cyan]")

    if auto:
        console.print("[green]Resuming...[/green]")
        return apply_state(cassette, state)

    console.print("[cyan]Resume? (Y/n):[/cyan] ", end="")
    try:
        answer = input().strip().lower()
    except EOFError:
        answer = "y"
    except KeyboardInterrupt:
        console.print("\nAborted.")
        sys.exit(130)

    if answer in ("", "y", "yes"):
        console.print("[green]Resuming...[/green]")
        return apply_state(cassette, state)
    else:
        clear_state()
        return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Workout Coach TUI")
    parser.add_argument("file", nargs="?", help="Workout file (.json cassette or .txt)")
    parser.add_argument(
        "--rest", type=int, default=None, metavar="N",
        help="Rest seconds. Text input defaults to 75; JSON cassettes keep their own "
             "rest unless this flag is passed explicitly (even --rest 75 overrides).",
    )
    parser.add_argument(
        "--volume", type=int, default=None, metavar="N",
        help="Master volume 0-100 for this run (overrides the persisted setting)",
    )
    parser.add_argument("--mute", action="store_true", help="Start muted (captions still show)")
    parser.add_argument(
        "--no-buddy", dest="buddy", action="store_false",
        help="Turn off Rep, the buddy coach panel. Persists for future runs — the buddy "
             "has no runtime key, so unlike --volume/--mute this flag saves the choice.",
    )
    parser.add_argument(
        "--buddy", dest="buddy", action="store_true",
        help="Turn the buddy coach panel back on (persisted, like --no-buddy)",
    )
    parser.set_defaults(buddy=None)
    parser.add_argument(
        "--only", metavar="NAME|N", default=None,
        help="Play a single group and exit: case-insensitive exercise-name substring, or "
             "1-based jump-menu row number. Logs a partial session; never touches (or "
             "resumes) a saved full-session state.",
    )
    parser.add_argument("--resume", action="store_true", help="Resume last workout without prompting")
    parser.add_argument("--reset", "--restart", action="store_true", help="Discard saved state and exit")
    parser.add_argument("--log", action="store_true", help="Print current saved log and exit")
    args = parser.parse_args()

    migrate_legacy_files()

    # Volume: persisted setting first, CLI flags override (for this run only —
    # they are not persisted; runtime key changes are).
    load_settings()
    user_settings.load_buddy_enabled()
    user_settings.load_paces()  # learned per-exercise pace, for ETAs
    if args.volume is not None:
        audio_settings.set_volume(args.volume / 100)
    if args.mute:
        audio_settings.muted = True
    if args.buddy is not None:
        user_settings.set_buddy_enabled(args.buddy)  # persists (no runtime buddy key)

    # args.rest is None unless --rest was passed; text input falls back to 75.
    rest = args.rest if args.rest is not None else 75

    if args.reset:
        clear_state()
        print("State cleared.")
        return

    if args.log:
        state = load_state_data()
        if not state:
            print("No saved state.")
            return
        if args.file:
            text = Path(args.file).read_text()
        else:
            saved_path = state.get("cassette_path", "")
            source = state.get("cassette_source", "")
            if saved_path and Path(saved_path).exists():
                text = Path(saved_path).read_text()
            elif source:
                text = source
            else:
                print("No saved cassette for log.", file=sys.stderr)
                sys.exit(1)
        cassette, _ = parse_input(text, rest)
        apply_state(cassette, state)
        print(render_log(cassette))
        return

    if args.only is not None:
        # --only: play one group, log it as a partial session, exit. This
        # path never calls try_resume and the player runs with state
        # persistence off, so a saved full-session state stays untouched.
        text = read_input(args.file)
        cassette, is_json = parse_input(text, rest)
        if not cassette.phases or not any(g for p in cassette.phases for g in p.groups):
            print("No exercises parsed. Check your input format.", file=sys.stderr)
            sys.exit(1)
        if not is_json or args.rest is not None:
            for phase in cassette.phases:
                for group in phase.groups:
                    group.rest = rest
        rows = build_jump_rows(cassette, -1, -1)
        matches = match_only(rows, args.only)
        if len(matches) != 1:
            kind = "No group matches" if not matches else "Multiple groups match"
            print(f"{kind} --only {args.only!r}. Groups:", file=sys.stderr)
            for i, row in enumerate(rows, 1):
                print(f"  {i}. {row.label}", file=sys.stderr)
            sys.exit(2)
        row = matches[0]
        play_only_group(cassette, row.phase_idx, row.group_idx, row.label)
        return

    cassette_path = None

    if args.resume:
        state = load_state_data()
        if state is None:
            print("No saved state to resume.", file=sys.stderr)
            sys.exit(1)
        # Load cassette: from file arg, saved path, or embedded source
        if args.file:
            cassette_path = args.file
            cassette, _ = parse_input(Path(args.file).read_text(), rest)
        else:
            saved_path = state.get("cassette_path", "")
            source = state.get("cassette_source", "")
            if saved_path and Path(saved_path).exists():
                cassette_path = saved_path
                cassette, _ = parse_input(Path(saved_path).read_text(), rest)
            elif source:
                cassette, _ = parse_input(source, rest)
            else:
                print("No saved cassette to resume from.", file=sys.stderr)
                sys.exit(1)
        # Go through try_resume so --resume gets the same safety checks as
        # the interactive prompt: cassette-hash mismatch and already-complete
        # states clear the stale state and start fresh instead of stamping
        # one cassette's progress onto another or re-logging forever.
        if try_resume(cassette, cassette_path, auto=True) is None:
            print("Nothing to resume; starting fresh.", file=sys.stderr)
    else:
        resumed_from_state = False
        # If no file given, check saved state to offer resume
        if not args.file:
            state = load_state_data()
            if state:
                saved_path = state.get("cassette_path", "")
                source = state.get("cassette_source", "")
                tmp_cassette = None
                tmp_path = None
                if saved_path and Path(saved_path).exists():
                    tmp_cassette, _ = parse_input(Path(saved_path).read_text(), rest)
                    tmp_path = saved_path
                elif source:
                    tmp_cassette, _ = parse_input(source, rest)
                if tmp_cassette:
                    resumed = try_resume(tmp_cassette, tmp_path)
                    if resumed is not None:
                        cassette = tmp_cassette
                        cassette_path = tmp_path
                        resumed_from_state = True

        if not resumed_from_state:
            text = read_input(args.file)
            cassette, is_json = parse_input(text, rest)

            if not cassette.phases or not any(g for p in cassette.phases for g in p.groups):
                print("No exercises parsed. Check your input format.", file=sys.stderr)
                sys.exit(1)

            if args.file:
                cassette_path = args.file

            # Apply --rest override to JSON cassettes only when the flag was
            # explicitly passed (including --rest 75). Text input already got
            # `rest` via text_to_cassette but the loop keeps it uniform.
            if not is_json or args.rest is not None:
                for phase in cassette.phases:
                    for group in phase.groups:
                        group.rest = rest

            # Try resume (when file was explicitly provided)
            try_resume(cassette, cassette_path)

    def _save_current_position():
        pos = {"phase_idx": 0, "group_idx": 0, "round_idx": 0}
        for pi, phase in enumerate(cassette.phases):
            for gi, group in enumerate(phase.groups):
                rc = rounds_completed(group)
                if rc < group.rounds and not group.skipped:
                    pos = {"phase_idx": pi, "group_idx": gi, "round_idx": rc}
                    break
        save_state(cassette, pos, cassette_path)

    while True:
        try:
            play_cassette(cassette, cassette_path)
            break
        except WorkoutPaused:
            restore_terminal()
            _save_current_position()
            terminate_say()
            print("\n\nWorkout paused. Progress saved. Type 'fg' to resume.\n")
            # Actually suspend the process
            signal.signal(signal.SIGTSTP, signal.SIG_DFL)
            os.kill(os.getpid(), signal.SIGTSTP)
            # --- execution resumes here after fg ---
            print("Resuming workout...\n")
            continue
        except KeyboardInterrupt:
            restore_terminal()
            _save_current_position()
            terminate_say()
            print("\n\nWorkout stopped. Progress saved.\n")
            print_log(cassette)
            save_log(cassette)
            break
