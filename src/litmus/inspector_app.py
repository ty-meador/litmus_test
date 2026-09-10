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

Textual is the terminal library by Ty's ruling of 2026-09-09 (issue #89): the readers
are asyncio-native, and its headless pilot lets these screens be driven by tests.
"""

from __future__ import annotations

from typing import ClassVar, Literal

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.widgets import Footer, Static

from litmus.inspector_run import SavedRun
from litmus.inspector_selection import InspectorSelection, SelectionView
from litmus.inspector_timeline import TimelineDiagnostic
from litmus.inspector_timeline_view import TimelineEntry, TimelineView
from litmus.inspector_tree import TreeRow

type Tab = Literal["inspect", "failures"]

# One style per recorded state. Aborted is neither red nor green because it is a
# distinct recovery path on the floor; a pending row is dim because null is not a
# fourth terminal state, only a node the run never reached (docs/inspector.md).
_STATE_STYLES: dict[str | None, tuple[str, str]] = {
    "Pass": ("Pass", "green"),
    "Fail": ("Fail", "bold red"),
    "Aborted": ("Aborted", "bold magenta"),
    None: ("Pending", "dim"),
}

_LEVEL_STYLES: dict[str, str] = {
    "error": "bold red",
    "critical": "bold red",
    "warning": "yellow",
}

LOG_HEADER_LINES = 2
"""The owner line and the page line that precede the chronology in the log pane."""


def row_text(row: TreeRow, *, selected: bool) -> Text:
    """Render one tree row: cursor, depth, name, state, and any reading not taken."""
    label, style = _STATE_STYLES[row.state]
    text = Text("> " if selected else "  ")
    text.append("  " * (len(row.ancestor_names) - 1))
    text.append(row.ancestor_names[-1], style="bold" if selected else "")
    text.append("  ")
    text.append(label, style=style)
    if row.not_taken:
        # A declared reading absent from the evidence was never taken; the guide asks
        # for it by name so the technician does not read the gap as a zero.
        text.append("  not taken: " + ", ".join(row.not_taken), style="yellow")
    return text


def window(count: int, selected: int, height: int) -> range:
    """The rows that fit around the selection when the tree is taller than the pane."""
    height = max(1, height)
    start = min(max(0, selected - height // 2), max(0, count - height))
    return range(start, min(count, start + height))


def log_header(view: SelectionView, timeline: TimelineView | None) -> tuple[Text, Text]:
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


def log_row_text(row: TimelineEntry | TimelineDiagnostic) -> Text:
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
    """Inspect and Failures over one saved run; the Run tab waits on its own decision."""

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
        Binding("tab", "cycle_tab", "Inspect / Failures", priority=True),
        Binding("up", "move(-1)", "Up", show=False),
        Binding("down", "move(1)", "Down", show=False),
        Binding("left", "failure(-1)", "Previous failure"),
        Binding("right", "failure(1)", "Next failure"),
        Binding("n", "page(1)", "Next page"),
        Binding("p", "page(-1)", "Previous page"),
        Binding("f", "toggle_errors", "Errors only"),
    ]

    def __init__(self, saved: SavedRun) -> None:
        super().__init__()
        self.saved = saved
        # Not ``self.tree``: Textual already uses that name for its widget tree.
        self.saved_tree = saved.tree
        self.selection = InspectorSelection(saved.tree, saved.run_root)
        self.tab: Tab = "inspect"
        self.timeline_view: TimelineView | None = None
        self._timeline_source: object = None
        self._page_size = 1
        self.tree_lines: tuple[str, ...] = ()
        self.log_lines: tuple[str, ...] = ()

    def compose(self) -> ComposeResult:
        yield Static(id="tabs")
        yield Static(id="tree")
        yield Static(id="log")
        yield Footer()

    async def on_mount(self) -> None:
        """Open on the root with its log loaded, so the first screen already answers."""
        self.sub_title = str(self.saved.run_root)
        await self.selection.select(0)
        self.redraw()

    def on_resize(self) -> None:
        self.redraw()

    async def action_cycle_tab(self) -> None:
        self.tab = "failures" if self.tab == "inspect" else "inspect"
        self.redraw()

    async def action_move(self, offset: int) -> None:
        """Up and down: through the tree, or through the failure list on that tab."""
        if self.tab == "failures":
            await self.action_failure(offset)
            return
        await self.selection.move(offset)
        self.redraw()

    async def action_failure(self, direction: int) -> None:
        if direction > 0:
            await self.selection.next_failure()
        else:
            await self.selection.previous_failure()
        self.redraw()

    def action_page(self, delta: int) -> None:
        if self.timeline_view is not None:
            if delta > 0:
                self.timeline_view.next_page()
            else:
                self.timeline_view.previous_page()
            self.redraw()

    def action_toggle_errors(self) -> None:
        if self.timeline_view is not None:
            self.timeline_view.toggle_errors()
            self.redraw()

    def redraw(self) -> None:
        """Render every pane from the models; the only place the screen reads them."""
        view = self.selection.view
        self._sync_timeline_view(view)
        tabs = Text()
        for name, label in (("inspect", "Inspect"), ("failures", "Failures")):
            tabs.append(f" {label} ", style="reverse" if self.tab == name else "")
            tabs.append("  ")
        tree_lines = self._tree_lines(view)
        log_lines = [*log_header(view, self.timeline_view)]
        if self.timeline_view is not None:
            log_lines.extend(log_row_text(row) for row in self.timeline_view.rows)
        self.tree_lines = tuple(line.plain for line in tree_lines)
        self.log_lines = tuple(line.plain for line in log_lines)
        self.query_one("#tabs", Static).update(tabs)
        self.query_one("#tree", Static).update(Text("\n").join(tree_lines))
        self.query_one("#log", Static).update(Text("\n").join(log_lines))

    def _tree_lines(self, view: SelectionView) -> list[Text]:
        rows = self.saved_tree.rows if self.tab == "inspect" else self.saved_tree.failures
        selected = view.selected.index
        if self.tab == "failures":
            position = next((i for i, row in enumerate(rows) if row.index == selected), 0)
            if not rows:
                return [Text("no Fail or Aborted node in this run", style="dim")]
        else:
            position = selected
        height = self.query_one("#tree", Static).size.height
        return [
            row_text(rows[index], selected=rows[index].index == selected)
            for index in window(len(rows), position, height)
        ]

    def _sync_timeline_view(self, view: SelectionView) -> None:
        """A fresh presentation model per loaded chronology; a resize keeps the current one."""
        page_size = max(1, self.query_one("#log", Static).size.height - LOG_HEADER_LINES)
        if view.timeline is None:
            self.timeline_view = None
            self._timeline_source = None
        elif view.timeline is not self._timeline_source:
            self.timeline_view = TimelineView(view.timeline, page_size=page_size)
            self._timeline_source = view.timeline
        elif self.timeline_view is not None and page_size != self._page_size:
            self.timeline_view.resize(page_size)
        self._page_size = page_size
