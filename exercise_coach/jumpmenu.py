"""Jump-menu model: pure, unit-testable menu state (recommendations §3).

`build_jump_rows`/`build_set_rows` compute the rows the menu shows,
`JumpMenuState.handle_key` is the cursor/expansion/digit-entry/confirm state
machine, and `render_jump_panel` turns the state into a Rich Panel. None of
them touch the terminal or the clock — the interactive loop lives in
screens.jump_menu_screen, and --only reuses the same rows via `match_only`.
"""

from dataclasses import dataclass

from rich.panel import Panel
from rich.text import Text

from .cassette import all_groups, rounds_completed
from .models import Cassette, ExerciseData, Group


@dataclass(frozen=True)
class JumpTarget:
    """A selected destination. Group-level when ex_idx/round_idx are None;
    set-level otherwise: exactly one exercise's set slot — for a superset
    that is one exercise of the round, not the whole round. redo=True means
    clear the group's progress first (already 'y'-confirmed in the menu)."""
    phase_idx: int
    group_idx: int
    ex_idx: int | None = None
    round_idx: int | None = None
    redo: bool = False


@dataclass(frozen=True)
class JumpRow:
    phase_idx: int
    group_idx: int
    label: str       # exercise names, " + " joined
    status: str      # "✓ done" / "2/3" / "skipped" / "→ current"
    completed: bool  # all rounds recorded: eligible for redo


@dataclass(frozen=True)
class SetRow:
    phase_idx: int
    group_idx: int
    ex_idx: int
    round_idx: int
    label: str       # "set 2 · Bench Press ×8"
    status: str      # "✓ 8" / "✗ 5 (failed)" / "" (pending)
    completed: bool


def build_jump_rows(cassette: Cassette, cur_phase: int, cur_group: int) -> list[JumpRow]:
    """One row per group across all phases, in cassette order. The playhead
    group shows "→ current" regardless of its progress."""
    rows = []
    for pi, gi, group in all_groups(cassette):
        label = " + ".join(ex.name for ex in group.exercises) or "(empty)"
        rc = rounds_completed(group)
        completed = bool(group.exercises) and rc >= group.rounds
        if (pi, gi) == (cur_phase, cur_group):
            status = "→ current"
        elif group.skipped:
            status = "skipped"
        elif completed or not group.exercises:
            status = "✓ done"
        else:
            status = f"{rc}/{group.rounds}"
        rows.append(JumpRow(pi, gi, label, status, completed))
    return rows


def _target_label(ex: ExerciseData, reps: int) -> str:
    return f"{reps}s" if ex.timed else str(reps)


def build_set_rows(phase_idx: int, group_idx: int, group: Group) -> list[SetRow]:
    """Per-set rows for one group, round-major: for a superset each row is
    one exercise's set slot in one round (the set-level jump unit)."""
    rows = []
    for r in range(group.rounds):
        for ei, ex in enumerate(group.exercises):
            if r >= len(ex.sets):
                continue
            s = ex.sets[r]
            if s.failure:
                status = f"✗ {s.actual_reps or 0} (failed)"
            elif s.actual_reps is not None:
                status = f"✓ {s.actual_reps}"
            else:
                status = ""
            rows.append(SetRow(
                phase_idx, group_idx, ei, r,
                label=f"set {r + 1} · {ex.name} ×{_target_label(ex, s.reps)}",
                status=status,
                completed=s.actual_reps is not None,
            ))
    return rows


def match_only(rows: list[JumpRow], spec: str) -> list[JumpRow]:
    """--only matching: all-digits = 1-based menu row index, anything else =
    case-insensitive substring of the row label. Returns every candidate so
    the caller can report ambiguity."""
    spec = spec.strip()
    # isascii() too: str.isdigit() accepts characters int() rejects ('²'…) —
    # those fall through to substring matching and get the friendly no-match
    # error instead of a ValueError crash.
    if spec.isascii() and spec.isdigit():
        n = int(spec)
        return [rows[n - 1]] if 1 <= n <= len(rows) else []
    needle = spec.lower()
    return [r for r in rows if needle in r.label.lower()]


# ---------------------------------------------------------------------------
# Key-driven menu state
# ---------------------------------------------------------------------------

# ("jump", target) / ("redo", target) / ("cancel", None)
Action = tuple[str, JumpTarget | None]


class JumpMenuState:
    """Pure key-driven menu state. `handle_key` returns an Action when a key
    resolves the menu, else None (the menu stays open)."""

    def __init__(self, cassette: Cassette, cur_phase: int, cur_group: int) -> None:
        self.cassette = cassette
        self.rows = build_jump_rows(cassette, cur_phase, cur_group)
        self.expanded: int | None = None  # row index whose set rows are shown
        self.digits = ""                  # typed row number (get_failure_reps pattern)
        self.confirm: int | None = None   # row index awaiting the redo 'y'
        self.cursor = 0                   # index into items()
        for i, row in enumerate(self.rows):
            if (row.phase_idx, row.group_idx) == (cur_phase, cur_group):
                self.cursor = i
                break

    def _group(self, row: JumpRow) -> Group:
        return self.cassette.phases[row.phase_idx].groups[row.group_idx]

    def items(self) -> list[tuple[str, object]]:
        """Flattened display list: ("group", row_idx) entries plus, under the
        expanded row, its ("set", SetRow) entries."""
        out: list[tuple[str, object]] = []
        for i, row in enumerate(self.rows):
            out.append(("group", i))
            if self.expanded == i:
                for sr in build_set_rows(row.phase_idx, row.group_idx, self._group(row)):
                    out.append(("set", sr))
        return out

    def handle_key(self, key: str) -> Action | None:
        if self.confirm is not None:
            # Redo is the only destructive action: it needs an explicit 'y';
            # any other key backs out of the confirmation.
            row = self.rows[self.confirm]
            self.confirm = None
            if key == "y":
                return ("redo", JumpTarget(row.phase_idx, row.group_idx, redo=True))
            return None
        if key in ("esc", "q"):
            return ("cancel", None)
        if key in ("up", "k"):
            self.cursor = max(0, self.cursor - 1)
            self.digits = ""
        elif key in ("down", "j"):  # 'j' means down *inside* the menu
            self.cursor = min(len(self.items()) - 1, self.cursor + 1)
            self.digits = ""
        elif key in ("right", "l"):
            self._expand()
        elif key == "left":
            self._collapse()
        elif key.isascii() and key.isdigit():
            # ASCII only: isdigit() also accepts '²' etc., which _select's
            # int() would crash on — non-ASCII digits are simply ignored.
            self.digits += key
        elif key == "\x7f":  # backspace
            self.digits = self.digits[:-1]
        elif key == "r":
            kind, val = self.items()[self.cursor]
            if kind == "group" and self.rows[val].completed:
                self.confirm = val
        elif key == "enter":
            return self._select()
        return None

    def _select(self) -> Action | None:
        if self.digits:
            n, self.digits = int(self.digits), ""
            if not 1 <= n <= len(self.rows):
                return None  # invalid row number: ignore, menu stays open
            row = self.rows[n - 1]
            return ("jump", JumpTarget(row.phase_idx, row.group_idx))
        kind, val = self.items()[self.cursor]
        if kind == "group":
            row = self.rows[val]
            return ("jump", JumpTarget(row.phase_idx, row.group_idx))
        if val.completed:
            # Replaying a recorded set would overwrite its result; redo is
            # the only destructive action, so a done set is not a target.
            return None
        return ("jump", JumpTarget(
            val.phase_idx, val.group_idx, ex_idx=val.ex_idx, round_idx=val.round_idx,
        ))

    def _expand(self) -> None:
        kind, val = self.items()[self.cursor]
        if kind != "group":
            return
        if self._group(self.rows[val]).exercises:
            self.expanded = val
            # Set rows insert below the group row, whose items() index is
            # exactly its row index while it is the (only) expanded row.
            self.cursor = val

    def _collapse(self) -> None:
        kind, val = self.items()[self.cursor]
        if kind == "set":
            self.cursor = self.expanded  # back onto the parent group row
            self.expanded = None
        elif kind == "group" and self.expanded == val:
            self.expanded = None
            self.cursor = val


# ---------------------------------------------------------------------------
# Renderer (pure: state in, Panel out)
# ---------------------------------------------------------------------------

_STATUS_STYLES = {"✓ done": "green", "skipped": "yellow", "→ current": "bold cyan"}


def render_jump_panel(state: JumpMenuState) -> Panel:
    """Build the jump-menu overlay Panel from rows + cursor + expansion +
    digit/confirm state. Text is assembled (not markup) so exercise names
    can never be misparsed as Rich tags."""
    body = Text()
    for idx, (kind, val) in enumerate(state.items()):
        if idx:
            body.append("\n")
        selected = idx == state.cursor
        marker = "❯ " if selected else "  "
        if kind == "group":
            row = state.rows[val]
            line = f"{marker}{val + 1:>2}. {row.label:<36} {row.status}"
            style = "bold white on blue" if selected else _STATUS_STYLES.get(row.status, "")
        else:
            line = f"{marker}      {val.label:<32} {val.status}"
            style = "bold white on blue" if selected else "dim"
        body.append(line, style=style or None)

    if state.confirm is not None:
        row = state.rows[state.confirm]
        group = state._group(row)
        n_recorded = sum(
            1 for ex in group.exercises for s in ex.sets if s.actual_reps is not None
        )
        body.append(
            f"\n\nRedo {row.label}? Clears {n_recorded} recorded set(s)"
            " — y = yes, any other key = no",
            style="bold red",
        )
    elif state.digits:
        body.append(f"\n\nGo to row: {state.digits}_", style="bold yellow")

    return Panel(
        body, title="Jump to", border_style="magenta", expand=True, padding=(1, 2),
        subtitle="↑/↓/k/j move • →/l sets • ← collapse • Enter jump • r redo • Esc/q cancel",
    )
