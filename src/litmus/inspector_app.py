"""The Inspector's terminal layer: render the saved-run models, forward keys, decide nothing.

``docs/inspector.md`` fixes the screen: a tab row, the structural tree above, the
highlighted node's chronology below and loaded live on cursor movement, and the key
bindings in its quick reference. Every model already exists and is proven against a
recorded 70-DUT rack without a terminal; this module adds only what a terminal must
add. Each key handler is one call into a model followed by a redraw, so the screen can
neither reach a different verdict than the record nor show one node's log under
another's name. The plain text of the last redraw is kept on the app for the self-tests
and for anyone reading a screenshot, because a screen that cannot be read back cannot
be held to the guide.

Two tabs, one cut between them (Ty's ruling of 2026-09-10): Inspect is the archive of
the production run and never executes anything; Re-Run is the bench, where Enter on a
node executes it again through ``litmus.inspector_rerun`` and nothing persists. The
legacy Failures list is folded into Inspect as the ``h`` key, which hides passing rows.

Textual is the terminal library by Ty's ruling of 2026-09-09 (issue #89): the readers
are asyncio-native, and its headless pilot lets these screens be driven by tests.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar, Literal

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.timer import Timer
from textual.widgets import Footer, Static

from litmus.inspector_artifact_view import ArtifactModal, DiffRow, diff_rows, single_rows
from litmus.inspector_rerun import RerunSession
from litmus.inspector_run import SavedRun
from litmus.inspector_selection import InspectorSelection, SelectionView
from litmus.inspector_timeline import TimelineDiagnostic
from litmus.inspector_timeline_view import TimelineEntry, TimelineView
from litmus.inspector_tree import TreeRow

type Tab = Literal["inspect", "rerun"]

# One style per recorded state. Aborted is neither red nor green because it is a
# distinct recovery path on the floor; a pending row is dim because null is not a
# fourth terminal state, only a node the run never reached (docs/inspector.md).
_STATE_STYLES: dict[str | None, tuple[str, str]] = {
    "Pass": ("Pass", "green"),
    "Fail": ("Fail", "bold red"),
    "Aborted": ("Aborted", "bold magenta"),
    None: ("Pending", "dim"),
}
_NOT_RERUN = ("not re-run", "dim")
_RUNNING_STYLE = "bold cyan"
_RUNNING_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

_LEVEL_STYLES: dict[str, str] = {
    "error": "bold red",
    "critical": "bold red",
    "warning": "yellow",
}

# The legacy tree glyphs, so depth is read from the line and not counted in spaces.
_TREE_BRANCH = "├─ "
_TREE_LAST = "└─ "
_TREE_VERT = "│  "
_TREE_INDENT = "   "
_OWNER_ARROW = "←"
"""Marks the ancestor whose timeline the highlighted node's records rolled into."""

_TAB_LABELS: tuple[tuple[Tab, str], ...] = (("inspect", "Inspect"), ("rerun", "Re-Run"))

LOG_HEADER_LINES = 2
"""The owner line and the page line that precede the chronology in the log pane."""


@dataclass(frozen=True, slots=True)
class RowDisplay:
    """Everything one tree line shows, resolved by the screen from the current tab.

    The label and readings come from the saved row on Inspect and from the re-run's
    row on Re-Run, so the same renderer never mixes archive and bench evidence.
    """

    name: str
    prefix: str
    label: str
    style: str
    not_taken: tuple[str, ...]
    selected: bool
    owner: bool


def _row_text(display: RowDisplay) -> Text:
    """Render one tree row: cursor, tree glyphs, name, state, readings not taken, owner."""
    text = Text("> " if display.selected else "  ")
    text.append(display.prefix, style="dim")
    text.append(display.name, style="bold" if display.selected else "")
    text.append("  ")
    text.append(display.label, style=display.style)
    if display.not_taken:
        # A declared reading absent from the evidence was never taken; the guide asks
        # for it by name so the technician does not read the gap as a zero.
        text.append("  not taken: " + ", ".join(display.not_taken), style="yellow")
    if display.owner:
        text.append(f"  {_OWNER_ARROW}", style="cyan")
    return text


def _tree_prefixes(depths: Sequence[int]) -> list[str]:
    """The glyph prefix of every displayed row, from the depths of the displayed rows.

    Computed over what is on screen rather than the full tree, so hiding passing rows
    still closes each branch on its last visible child. A row is the last of its
    siblings when no later row at its depth appears before a shallower one.
    """
    last = [False] * len(depths)
    continuing: set[int] = set()
    for position in range(len(depths) - 1, -1, -1):
        depth = depths[position]
        last[position] = depth not in continuing
        continuing = {other for other in continuing if other < depth}
        continuing.add(depth)
    prefixes: list[str] = []
    last_at_depth: dict[int, bool] = {}
    for position, depth in enumerate(depths):
        last_at_depth[depth] = last[position]
        if depth == 0:
            prefixes.append("")
            continue
        lines = "".join(
            _TREE_INDENT if last_at_depth.get(level, True) else _TREE_VERT
            for level in range(1, depth)
        )
        prefixes.append(lines + (_TREE_LAST if last[position] else _TREE_BRANCH))
    return prefixes


def _window(count: int, selected: int, height: int) -> range:
    """The rows that fit around the selection when the tree is taller than the pane."""
    height = max(1, height)
    start = min(max(0, selected - height // 2), max(0, count - height))
    return range(start, min(count, start + height))


def _log_header(view: SelectionView, timeline: TimelineView | None) -> tuple[Text, Text]:
    """The two lines above the chronology: whose log this is, and where the page is."""
    owner = Text("log: ")
    if view.status == "ready" and view.owner is not None:
        owner.append("/".join(view.owner.ancestor_names))
        if view.owner.index != view.selected.index:
            owner.append(f"  (rolled up: {view.selected.ancestor_names[-1]} has no own log)", "dim")
    elif view.status == "loading":
        owner.append("loading", "dim")
    elif view.status == "not-run":
        owner.append("not run: the node never executed", "dim")
    else:
        owner.append(f"{view.status}: {view.message or 'no chronology'}", "yellow")
    page = Text()
    if timeline is not None:
        page.append(f"page {timeline.page_index + 1}/{timeline.page_count}")
        page.append("  errors only: " + ("on" if timeline.errors_only else "off"))
        page.append(f"  captures: {timeline.artifact_count}")
        if timeline.artifact_count:
            page.append("  (1-9 opens a capture)", "dim")
    return owner, page


def _log_row_text(row: TimelineEntry | TimelineDiagnostic) -> Text:
    """Render one chronology row, marking captures by number and damage in place."""
    if isinstance(row, TimelineDiagnostic):
        # Damage stays where the reader found it, never hidden or filled in. The line
        # number is the physical line of the saved file so the technician can open it.
        where = "" if row.line_number is None else f" at line {row.line_number}"
        return Text(f"--- {row.kind}{where}: {row.message} ---", style="bold yellow")
    data = row.record.data
    text = Text(f"[{row.artifact_number}] " if row.artifact_number is not None else "    ")
    text.append(str(data.get("timestamp", "")), style="dim")
    level = str(data.get("level", ""))
    text.append(f" {level:<8} ", style=_LEVEL_STYLES.get(level.lower(), ""))
    text.append(str(data.get("message", "")))
    return text


class InspectorApp(App[None]):
    """Inspect and Re-Run over one saved run; the session behind Re-Run owns execution."""

    TITLE = "litmus inspect"
    CSS = """
    #tabs { height: 1; padding: 0 1; background: $surface; }
    #tree { height: 1fr; padding: 0 1; border-bottom: solid $secondary; }
    #log { height: 1fr; padding: 0 1; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "quit", "Quit", priority=True),
        # Textual's own ctrl+c only offers to quit; the guide says it quits, with
        # nothing at stake in a saved inspection.
        Binding("ctrl+c", "quit", "Quit", priority=True, show=False),
        # Priority, because Textual's screen otherwise takes Tab for focus movement;
        # the guide gives Tab to the tab row.
        Binding("tab", "cycle_tab", "Inspect / Re-Run", priority=True),
        Binding("up", "move(-1)", "Up", show=False),
        Binding("down", "move(1)", "Down", show=False),
        Binding("left", "failure(-1)", "Previous failure"),
        Binding("right", "failure(1)", "Next failure"),
        Binding("h", "toggle_passes", "Hide passes"),
        Binding("enter", "rerun", "Re-run node", priority=True),
        Binding("n", "page(1)", "Next page"),
        Binding("p", "page(-1)", "Previous page"),
        Binding("f", "toggle_errors", "Errors only"),
        # Digits open the capture carrying that number in the complete chronology;
        # the number never changes with paging or filtering (TimelineView).
        *[
            Binding(str(digit), f"open_capture({digit})", "Open capture", show=False)
            for digit in range(1, 10)
        ],
    ]

    def __init__(self, saved: SavedRun, session: RerunSession | None = None) -> None:
        super().__init__()
        self.saved = saved
        # Not ``self.tree``: Textual already uses that name for its widget tree.
        self.saved_tree = saved.tree
        self.selection = InspectorSelection(saved.tree, saved.run_root)
        # A screen opened without a session still has the tab, and the tab says why
        # it cannot run: the operator supplied no suite through the command line.
        self.session = (
            session
            if session is not None
            else RerunSession(saved, "no suite was supplied to this Inspector")
        )
        self.tab: Tab = "inspect"
        self.hide_passes = False
        self.timeline_view: TimelineView | None = None
        self._timeline_source: object = None
        self._page_size = 1
        self._quit_after_run = False
        self._spin = 0
        self._ticker: Timer | None = None
        self.tree_lines: tuple[str, ...] = ()
        self.log_lines: tuple[str, ...] = ()

    def compose(self) -> ComposeResult:
        """Three panes and a footer; the layout is fixed and only the content is redrawn."""
        yield Static(id="tabs")
        yield Static(id="tree")
        yield Static(id="log")
        yield Footer()

    async def on_mount(self) -> None:
        """Open on the root with its log loaded, so the first screen already answers."""
        self.sub_title = str(self.saved.run_root)
        await self.selection.select(0)
        self.redraw()

    def on_unmount(self) -> None:
        """Scratch evidence goes with the screen; the command line closes it again, harmlessly."""
        self.session.close()

    def on_resize(self) -> None:
        """Redraw so the tree and log fit the new height; nothing else changes."""
        self.redraw()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Offer each tab only its own keys, and nothing that would disturb a live re-run."""
        running = self.session.running is not None
        if action == "rerun":
            if self.tab != "rerun":
                return None
            return self.session.available and not running
        if action == "toggle_passes":
            return None if self.tab != "inspect" else True
        if action == "cycle_tab":
            return not running
        return True

    async def action_quit(self) -> None:
        """Quit at once from a saved view; from a live re-run, interrupt and quit once it settles.

        Quitting live hardware execution is not the harmless act it is on the archive:
        the run must reach Aborted through bounded cleanup before the terminal is given
        back (``docs/inspector.md``, the Re-Run tab).
        """
        if self.session.running is None:
            self.exit()
            return
        self._quit_after_run = True
        self.session.interrupt()
        self.redraw()

    async def action_cycle_tab(self) -> None:
        """Tab toggles Inspect and Re-Run; the first entry to Re-Run loads the suite."""
        if self.session.running is not None:
            return
        self.tab = "rerun" if self.tab == "inspect" else "inspect"
        if self.tab == "rerun":
            await self.session.prepare()
            await self._sync_rerun_log()
        self.redraw()

    async def action_toggle_passes(self) -> None:
        """Hide or show passing rows on Inspect, keeping the cursor on something visible."""
        if self.tab != "inspect":
            return
        self.hide_passes = not self.hide_passes
        visible = self._visible_rows()
        selected = self.selection.view.selected.index
        if visible and all(row.index != selected for row in visible):
            # The highlighted row just disappeared: land on the nearest row above it,
            # or the first below, rather than on a row the technician cannot see.
            above = [row for row in visible if row.index < selected]
            target = above[-1] if above else visible[0]
            await self.selection.select(target.index)
        self.redraw()

    async def action_move(self, offset: int) -> None:
        """Up and down through the rows the tab shows; hidden rows are stepped over."""
        visible = self._visible_rows()
        if not visible:
            return
        selected = self.selection.view.selected.index
        position = next((i for i, row in enumerate(visible) if row.index == selected), 0)
        target = min(max(0, position + offset), len(visible) - 1)
        await self.selection.select(visible[target].index)
        await self._sync_rerun_log()
        self.redraw()

    async def action_failure(self, direction: int) -> None:
        """Jump between failed nodes, so the next problem is reached without walking the tree."""
        if direction > 0:
            await self.selection.next_failure()
        else:
            await self.selection.previous_failure()
        await self._sync_rerun_log()
        self.redraw()

    def action_rerun(self) -> None:
        """Enter on the Re-Run tab executes the highlighted node; nothing else ever does."""
        if self.tab != "rerun" or self.session.running is not None or not self.session.available:
            return
        index = self.selection.view.selected.index
        self._ticker = self.set_interval(0.2, self._tick)
        self.run_worker(self._execute(index), group="rerun", exclusive=True, exit_on_error=False)
        self.redraw()

    async def _execute(self, index: int) -> None:
        try:
            await self.session.run(index, on_start=self.redraw)
            await self._sync_rerun_log()
        finally:
            if self._ticker is not None:
                self._ticker.stop()
                self._ticker = None
            self.redraw()
            if self._quit_after_run:
                self.exit()

    def _tick(self) -> None:
        self._spin += 1
        self.redraw()

    def action_page(self, delta: int) -> None:
        """Page the log; before a timeline is loaded there is nothing to page."""
        if self.timeline_view is not None:
            if delta > 0:
                self.timeline_view.next_page()
            else:
                self.timeline_view.previous_page()
            self.redraw()

    def action_toggle_errors(self) -> None:
        """Show only error rows, or all rows again; the timeline view decides what counts."""
        if self.timeline_view is not None:
            self.timeline_view.toggle_errors()
            self.redraw()

    def action_open_capture(self, number: int) -> None:
        """Open the numbered capture in the modal; a digit with no capture does nothing."""
        if self.timeline_view is None or self.timeline_view.artifact(number) is None:
            return
        modal = ArtifactModal(
            self.timeline_view, self.saved.artifacts, initial_number=number, page_size=1
        )
        self.push_screen(ArtifactScreen(modal))

    def redraw(self) -> None:
        """Render every pane from the models; the only place the screen reads them."""
        view = self.selection.view if self.tab == "inspect" else self.session.view
        self._sync_timeline_view(view)
        tabs = Text()
        for name, label in _TAB_LABELS:
            tabs.append(f" {label} ", style="reverse" if self.tab == name else "")
            tabs.append("  ")
        tree_lines = self._tree_lines(view)
        log_lines = [*self._log_header(view)]
        if self.timeline_view is not None:
            log_lines.extend(_log_row_text(row) for row in self.timeline_view.rows)
        self.tree_lines = tuple(line.plain for line in tree_lines)
        self.log_lines = tuple(line.plain for line in log_lines)
        self.query_one("#tabs", Static).update(tabs)
        self.query_one("#tree", Static).update(Text("\n").join(tree_lines))
        self.query_one("#log", Static).update(Text("\n").join(log_lines))

    def _log_header(self, view: SelectionView | None) -> tuple[Text, Text]:
        if view is not None:
            return _log_header(view, self.timeline_view)
        # Re-Run with nothing to show for the highlighted node: say what the bench is
        # doing, or what it is waiting for, rather than an empty pane.
        owner = Text("log: ")
        running = self.session.running
        if running is not None:
            name = "/".join(self.saved_tree.rows[running].ancestor_names)
            owner.append(f"running {name}", _RUNNING_STYLE)
        elif self.session.diagnostic is not None:
            owner.append(self.session.diagnostic, "yellow")
        elif not self.session.available:
            owner.append(f"re-run unavailable: {self.session.unavailable}", "yellow")
        else:
            owner.append("not re-run yet: press Enter on a node to execute it", "dim")
        return owner, Text()

    def _visible_rows(self) -> tuple[TreeRow, ...]:
        rows = self.saved_tree.rows
        if self.tab == "inspect" and self.hide_passes:
            return tuple(row for row in rows if row.state != "Pass")
        return rows

    def _tree_lines(self, view: SelectionView | None) -> list[Text]:
        rows = self._visible_rows()
        if not rows:
            return [Text("every node passed; h shows them again", style="dim")]
        selected = self.selection.view.selected.index
        position = next((i for i, row in enumerate(rows) if row.index == selected), 0)
        height = self.query_one("#tree", Static).size.height
        prefixes = _tree_prefixes([len(row.ancestor_names) - 1 for row in rows])
        owner: int | None = None
        if view is not None and view.owner is not None and view.owner.index != view.selected.index:
            owner = view.owner.index
        running = self.session.running_indices() if self.tab == "rerun" else range(0)
        lines: list[Text] = []
        for index in _window(len(rows), position, height):
            row = rows[index]
            evidence: TreeRow | None = row
            if self.tab == "rerun":
                record = self.session.record(row.index)
                evidence = None if record is None else record.row
            if row.index in running:
                frame = _RUNNING_FRAMES[self._spin % len(_RUNNING_FRAMES)]
                label, style = f"{frame} Running", _RUNNING_STYLE
            elif evidence is None:
                label, style = _NOT_RERUN
            else:
                label, style = _STATE_STYLES[evidence.state]
            lines.append(
                _row_text(
                    RowDisplay(
                        name=row.ancestor_names[-1],
                        prefix=prefixes[index],
                        label=label,
                        style=style,
                        not_taken=() if evidence is None else evidence.not_taken,
                        selected=row.index == selected,
                        owner=row.index == owner,
                    )
                )
            )
        return lines

    async def _sync_rerun_log(self) -> None:
        """Keep the Re-Run log on the highlighted node; Inspect keeps its own selection."""
        if self.tab == "rerun":
            await self.session.select(self.selection.view.selected.index)

    def _sync_timeline_view(self, view: SelectionView | None) -> None:
        """A fresh presentation model per loaded chronology; a resize keeps the current one."""
        page_size = max(1, self.query_one("#log", Static).size.height - LOG_HEADER_LINES)
        timeline = None if view is None else view.timeline
        if timeline is None:
            self.timeline_view = None
            self._timeline_source = None
        elif timeline is not self._timeline_source:
            self.timeline_view = TimelineView(timeline, page_size=page_size)
            self._timeline_source = timeline
        elif self.timeline_view is not None and page_size != self._page_size:
            self.timeline_view.resize(page_size)
        self._page_size = page_size


class ArtifactScreen(ModalScreen[None]):
    """The artifact modal over the main view: one capture, or two side by side.

    The modal owns its keys until it closes, as the guide's artifact table specifies:
    ``[`` and ``]`` move between types (or the left side of a comparison), the arrows
    between captures of the type (or the right side), ``n`` and ``p`` page both sides
    together, ``d`` enters and leaves the comparison, ``b`` returns to the log. Every
    handler is one call into ``ArtifactModal``; loading, paging, alignment, and the
    swap that keeps two targets distinct all live there.
    """

    CSS = """
    ArtifactScreen { align: center middle; }
    #modal { width: 92%; height: 90%; padding: 0 1; }
    #modal { border: thick $secondary; background: $surface; }
    #modal-title { height: 2; }
    #modal-body { height: 1fr; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("b", "close", "Back to log", priority=True),
        Binding("escape", "close", "Back to log", show=False, priority=True),
        Binding("left_square_bracket", "step_type(-1)", "Prev type / left", priority=True),
        Binding("right_square_bracket", "step_type(1)", "Next type / left", priority=True),
        Binding("left", "step_artifact(-1)", "Prev capture / right", priority=True),
        Binding("right", "step_artifact(1)", "Next capture / right", priority=True),
        Binding("n", "page(1)", "Next page", priority=True),
        Binding("p", "page(-1)", "Previous page", priority=True),
        Binding("d", "toggle_diff", "Diff", priority=True),
        Binding("q", "app.quit", "Quit", priority=True),
        Binding("ctrl+c", "app.quit", "Quit", priority=True, show=False),
    ]

    def __init__(self, modal: ArtifactModal) -> None:
        super().__init__()
        self.modal = modal
        self.modal_lines: tuple[str, ...] = ()

    def compose(self) -> ComposeResult:
        """A title over a body; the modal model supplies both."""
        with Vertical(id="modal"):
            yield Static(id="modal-title")
            yield Static(id="modal-body")

    async def on_mount(self) -> None:
        """Load the opened capture only once the screen knows its height."""
        self._fit()
        await self.modal.open()
        self.redraw()

    def on_resize(self) -> None:
        """Re-page to the new height so the first visible row stays in view."""
        self._fit()
        self.redraw()

    def _fit(self) -> None:
        """Page by the body's height; before layout that is one line, corrected on resize."""
        self.modal.resize(max(1, self.query_one("#modal-body", Static).size.height))

    def action_close(self) -> None:
        """Return to the main view; the modal holds nothing worth saving."""
        self.dismiss()

    async def action_step_type(self, direction: int) -> None:
        """Move between capture types (`docs/inspector.md`, reading and comparing artifacts)."""
        if direction > 0:
            await self.modal.next_type()
        else:
            await self.modal.previous_type()
        self.redraw()

    async def action_step_artifact(self, direction: int) -> None:
        """Move between captures of the current type."""
        if direction > 0:
            await self.modal.next_artifact()
        else:
            await self.modal.previous_artifact()
        self.redraw()

    def action_page(self, delta: int) -> None:
        """Page the body; the model clamps at either end."""
        if delta > 0:
            self.modal.next_page()
        else:
            self.modal.previous_page()
        self.redraw()

    async def action_toggle_diff(self) -> None:
        """Enter or leave side-by-side comparison; the model decides whether a pair exists."""
        await self.modal.toggle_diff()
        self.redraw()

    def redraw(self) -> None:
        """Render the title and body from the modal model; nothing is decided here."""
        title = _modal_title(self.modal)
        body = _modal_body(self.modal, self.query_one("#modal-body", Static).size.width)
        self.modal_lines = (*title.plain.split("\n"), *(line.plain for line in body))
        self.query_one("#modal-title", Static).update(title)
        self.query_one("#modal-body", Static).update(Text("\n").join(body))


def _modal_title(modal: ArtifactModal) -> Text:
    """Two lines: where the selection sits among types and captures, and the page."""
    navigation = modal.navigation
    types = navigation.types
    numbers = navigation.numbers
    title = Text()
    title.append(
        f"type {types.index(navigation.artifact_type) + 1}/{len(types)}: "
        f"{navigation.artifact_type}   "
    )
    if modal.diff and modal.left is not None and modal.right is not None:
        title.append("diff  ", style="bold")
        title.append(modal.left.title, style="bold")
        title.append("  |  ")
        title.append(modal.right.title, style="bold")
    elif modal.left is not None:
        current = numbers.index(navigation.current.number) + 1
        title.append(f"capture {current}/{len(numbers)}   ")
        title.append(modal.left.title, style="bold")
    else:
        title.append("loading", style="dim")
    title.append(f"\npage {modal.page_index + 1}/{modal.page_count}")
    title.append("   b back  [ ] type  ← → capture  n p page  d diff", style="dim")
    return title


def _modal_body(modal: ArtifactModal, width: int) -> list[Text]:
    """The current page: one capture's lines, or two aligned columns of a comparison."""
    if modal.status != "ready":
        return [Text("loading", style="dim")]
    if not modal.diff:
        lines = [Text(line) for line in single_rows(modal)]
        if modal.left is not None and modal.left.diagnostic is not None:
            for line in lines:
                line.stylize("bold yellow")
        return lines
    column = max(8, (max(20, width) - 3) // 2)
    return [_diff_line(row, column) for row in diff_rows(modal)]


def _diff_line(row: DiffRow, column: int) -> Text:
    """Two columns of one aligned row; a differing row is highlighted, a gap is blank."""
    style = "" if row.same else "bold yellow"
    text = Text()
    text.append(_column(row.left, column), style=style)
    text.append(" | ", style="dim")
    text.append(_column(row.right, column), style=style)
    return text


def _column(cell: str | None, column: int) -> str:
    """Fit one side to its column so the two sides stay aligned."""
    return ("" if cell is None else cell)[:column].ljust(column)
