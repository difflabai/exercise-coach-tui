"""TUI rendering: overview table, panels, progress bar, layout composition."""

from rich import box
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .cassette import count_sets, rounds_completed
from .models import Cassette, ExerciseData, Group
from .tts import CAPTION_DURATION, current_caption


def format_rep_target(
    ex: ExerciseData, reps: int, *,
    timed_suffix: str = "", reps_suffix: str = "",
) -> str:
    """Display label for a set's rep target, with the v1.2 transforms.

    Base is "12" (reps) or "30s" (timed), plus the caller's context suffix
    (" reps" / " hold"). `per_side` turns the base into "12/side" / "30s/side"
    (the suffix is dropped — "/side" already reads as a complete target).
    `tempo` is appended after " · ". Display-only: log output never uses this.
    """
    if ex.timed:
        label = f"{reps}s"
        suffix = timed_suffix
    else:
        label = str(reps)
        suffix = reps_suffix
    if ex.per_side:
        label += "/side"
    elif suffix:
        label += suffix
    if ex.tempo:
        label += f" · {ex.tempo}"
    return label


def build_overview(cassette: Cassette, cur_phase: int, cur_group: int) -> Table:
    """Build the overview table showing all phases, groups, and exercises."""
    title = cassette.meta.get("title", "Workout")
    program = cassette.meta.get("program", "")
    if program:
        title = f"{title} — {program}"

    table = Table(
        title=title, box=box.SIMPLE_HEAVY, show_header=False,
        pad_edge=False, expand=True,
    )
    table.add_column("Exercise", ratio=1)

    for pi, phase in enumerate(cassette.phases):
        # Phase header
        table.add_row(Text(f" {phase.type.upper()}", style="bold underline"))

        for gi, group in enumerate(phase.groups):
            is_current = pi == cur_phase and gi == cur_group
            n_ex = len(group.exercises)
            rc = rounds_completed(group)

            for ei, ex in enumerate(group.exercises):
                reps_str = format_rep_target(ex, ex.sets[0].reps)
                load_str = f" | {ex.load}" if ex.load else ""

                if group.skipped:
                    label = f"{ex.name:<25} {group.rounds}[0]×{reps_str}{load_str}"
                    style = "dim yellow"
                elif rc >= group.rounds:
                    label = f"{ex.name:<25} {group.rounds}[{rc}]×{reps_str}{load_str}"
                    style = "dim green"
                elif is_current:
                    label = f"{ex.name:<25} {group.rounds}[{rc}]×{reps_str}{load_str}"
                    style = "bold white on blue"
                else:
                    label = f"{ex.name:<25} {group.rounds}×{reps_str}{load_str}"
                    style = ""

                # Superset/circuit connectors
                prefix = "  "
                if n_ex > 1:
                    if ei == 0:
                        prefix = "  ┌ "
                    elif ei == n_ex - 1:
                        prefix = "  └ "
                    else:
                        prefix = "  ├ "
                else:
                    prefix = "    "

                text = Text(f"{prefix}{label}")
                if style:
                    text.stylize(style)
                table.add_row(text)

        table.add_row(Text(""))  # spacing between phases

    # Context exercises
    if cassette.context_exercises:
        table.add_row(Text(" ── Context ──", style="dim"))
        for ctx in cassette.context_exercises:
            table.add_row(Text(f"    {ctx.name}: {ctx.note}", style="dim"))

    return table


def build_active_panel_straight(
    ex: ExerciseData, round_idx: int, total_rounds: int,
    status: str = "", timer_text: str = "", timer_style: str = "bold white",
) -> Panel:
    """Active panel for straight sets (single exercise group)."""
    lines: list[str] = []
    reps_label = format_rep_target(
        ex, ex.sets[round_idx].reps, timed_suffix=" hold", reps_suffix=" reps",
    )
    lines.append(f"[bold]{ex.name}[/bold]")
    if ex.load:
        lines.append(f"Weight: {ex.load}")
    lines.append(f"Round {round_idx + 1} of {total_rounds}  •  {reps_label}")
    if status:
        lines.append(f"\n{status}")
    if timer_text:
        lines.append(f"\n[{timer_style}]{timer_text}[/{timer_style}]")
    return Panel("\n".join(lines), title="Current", border_style="cyan", expand=True)


def build_active_panel_superset(
    group: Group, round_idx: int, active_ex_idx: int,
    status: str = "", timer_text: str = "", timer_style: str = "bold white",
) -> Panel:
    """Active panel for supersets/circuits showing all exercises with markers."""
    lines: list[str] = []
    for ei, ex in enumerate(group.exercises):
        reps_label = format_rep_target(ex, ex.sets[round_idx].reps, reps_suffix=" reps")
        load_str = f"  •  {ex.load}" if ex.load else ""
        if ei < active_ex_idx:
            marker = "✓"
        elif ei == active_ex_idx:
            marker = "►"
        else:
            marker = " "
        lines.append(f"{marker} {ex.name}  •  {reps_label}{load_str}")
    if status:
        lines.append(f"\n{status}")
    if timer_text:
        lines.append(f"\n[{timer_style}]{timer_text}[/{timer_style}]")
    title = f"Superset (Round {round_idx + 1} of {group.rounds})" if group.type == "superset" else f"Circuit (Round {round_idx + 1} of {group.rounds})"
    return Panel("\n".join(lines), title=title, border_style="cyan", expand=True)


def build_active_panel(
    cassette: Cassette, group: Group, ex: ExerciseData,
    round_idx: int, ex_idx: int,
    status: str = "", timer_text: str = "", timer_style: str = "bold white",
) -> Panel:
    """Route to the right panel type based on group structure."""
    if len(group.exercises) == 1:
        return build_active_panel_straight(ex, round_idx, group.rounds, status, timer_text, timer_style)
    return build_active_panel_superset(group, round_idx, ex_idx, status, timer_text, timer_style)


def build_rest_panel(rest_seconds: int, remaining: float, overtime: bool) -> Panel:
    """Panel shown during rest periods."""
    if overtime:
        ot_secs = int(-remaining)
        timer_text = f"OVERTIME +{ot_secs}s  (Enter to continue • b = back • p = pause)"
        timer_style = "bold red"
    else:
        secs_left = int(remaining) + 1
        timer_text = f"{secs_left}s remaining  (Enter to skip • b = back • p = pause)"
        timer_style = "bold yellow"
    content = f"Resting...\n\n[{timer_style}]{timer_text}[/{timer_style}]"
    return Panel(content, title="Rest", border_style="yellow", expand=True)


def format_eta(seconds: int) -> str:
    if seconds <= 0:
        return "done"
    minutes, secs = divmod(seconds, 60)
    if minutes > 0:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def estimate_remaining(cassette: Cassette, avg_rep_set: float = 30.0) -> int:
    """Estimate seconds remaining based on cassette state."""
    remaining = 0
    remaining_sets = 0
    rest_total = 0
    for phase in cassette.phases:
        for group in phase.groups:
            if group.skipped:
                continue
            for ex in group.exercises:
                for s in ex.sets:
                    if s.actual_reps is None:
                        if ex.timed:
                            remaining += s.reps + 3  # hold + countdown
                        else:
                            remaining += int(avg_rep_set)
                        remaining_sets += 1
            # Estimate rest for remaining rounds
            rc = rounds_completed(group)
            rounds_left = group.rounds - rc
            if rounds_left > 0:
                rest_total += (rounds_left - 1) * group.rest
    remaining += rest_total
    return remaining


def build_progress_bar(cassette: Cassette, avg_rep_set: float = 30.0) -> str:
    total, done = count_sets(cassette)
    pct = done / total * 100 if total else 100
    bar_len = 30
    filled = int(bar_len * done / total) if total else bar_len
    bar = "█" * filled + "░" * (bar_len - filled)
    eta = format_eta(estimate_remaining(cassette, avg_rep_set))
    return f"[bold cyan]Progress:[/bold cyan] {bar} {done}/{total} sets ({pct:.0f}%)  ⏱ ETA: {eta}"


def render_layout(live: Live, overview: Table, panel: Panel, progress_text: str) -> None:
    """Compose and push a full screen update."""
    layout = Table.grid(expand=True)
    layout.add_row(overview)
    layout.add_row(panel)
    layout.add_row(Text.from_markup(progress_text))

    # Caption subtitle — shows what TTS just said
    caption, age = current_caption()
    if caption and age < CAPTION_DURATION:
        if age < CAPTION_DURATION * 0.75:
            style = "bold italic bright_white on grey23"
        else:
            style = "italic grey62"
        caption_text = Text(f" 🗣  {caption} ", style=style, justify="center")
        layout.add_row(caption_text)

    live.update(layout)
