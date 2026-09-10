"""Join the saved tree cursor to contained chronology for Inspect and Failures.

This mutable view model owns selection-load state, not verdicts or terminal rendering.
Every load retains the selected claim and its recorded log owner together. Superseded
reads may finish, but cannot publish evidence beneath a different highlighted claim.
"""

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, cast

from litmus.inspector_timeline import TimelineDiagnostic, TimelineRead, TimelineReader
from litmus.inspector_tree import InspectorTree, TreeRow


@dataclass(frozen=True, slots=True)
class SelectionView:
    """One selection and its load state; reader diagnostics never change the saved state.

    ``idle`` means no load has completed (including cancellation or a raised reader
    defect). ``loading`` carries no previous chronology. ``ready`` may include decoding
    diagnostics beside records; ``unavailable`` identifies a missing owner or file and
    retains any partial read. ``not-run`` is reserved for a pending recorded node.
    """

    selected: TreeRow
    owner: TreeRow | None
    status: Literal["idle", "loading", "ready", "unavailable", "not-run"]
    timeline: TimelineRead | None = None
    message: str | None = None


class InspectorSelection:
    """Own a mutable selection over one validated tree and its opened run directory.

    The caller supplies a tree from TreeReader and the directory containing that same
    selected result. Navigate through this model rather than mutating the tree cursor
    separately; failure-list rows use the tree's same stable indices. Call select for
    the initial load and on cursor changes. All methods run on one event loop; concurrent
    loads are permitted, and only the latest request may update ``view``.
    """

    def __init__(
        self, tree: InspectorTree, run_root: Path, *, reader: TimelineReader | None = None
    ) -> None:
        self._tree = tree
        self._run_root = run_root
        self._reader = reader if reader is not None else TimelineReader()
        self._view = SelectionView(tree.selected, tree.log_owner, "idle")

    @property
    def view(self) -> SelectionView:
        """The current claim and chronology, replaced together without changing saved data."""
        return self._view

    async def select(self, index: int) -> None:
        """Select and load a row; invalid indices raise IndexError without changing the view.

        The old log is cleared before file I/O. Reselecting a row reads evidence afresh.
        Cancellation and reader defects propagate, returning the current load to idle;
        neither a late success nor a late error can disturb a subsequent selection.
        """
        self._tree.select(index)
        selected, owner = self._tree.selected, self._tree.log_owner
        if selected.state is None:
            self._view = SelectionView(selected, None, "not-run")
            return
        if owner is None:
            self._view = SelectionView(
                selected, None, "unavailable", message="log unavailable: no recorded timeline owner"
            )
            return

        pending = SelectionView(selected, owner, "loading")
        self._view = pending
        try:
            timeline = await self._reader.read_reference(
                self._run_root, cast(str, owner.record["log_path"])
            )
        except BaseException:
            # A cancelled worker may finish independently. Clear only this request's
            # loading state, and never turn a packaging defect into damaged evidence.
            if self._view is pending:
                self._view = replace(pending, status="idle")
            raise
        if self._view is not pending:
            return
        unavailable = any(
            isinstance(row, TimelineDiagnostic) and row.kind == "unavailable"
            for row in timeline.rows
        )
        self._view = replace(
            pending, status="unavailable" if unavailable else "ready", timeline=timeline
        )

    async def move(self, offset: int) -> None:
        """Move by a signed row count, clamp at either end, and load that selection."""
        self._tree.move(offset)
        await self.select(self._tree.selected.index)

    async def next_failure(self) -> None:
        """Load the next Fail or Aborted row without wrapping or recalculating a verdict."""
        self._tree.next_failure()
        await self.select(self._tree.selected.index)

    async def previous_failure(self) -> None:
        """Load the previous Fail or Aborted row without wrapping or changing its meaning."""
        self._tree.previous_failure()
        await self.select(self._tree.selected.index)
