"""Re-run one node of a saved run from the Inspector, through the sealed lifecycle.

Ty's ruling of 2026-09-10 (issue #89) gave the Inspector two tabs with one cut between
them. Inspect is the archive of the production run and executes nothing. Re-Run is the
technician's bench: any node of the same tree can be executed again with the code that
ran in production, so a reseated cable or a replaced part is checked in seconds before
the whole suite is restarted, and nothing the bench produces persists. This module is
the bench.

Three rules keep it honest. The suite is supplied by the operator through ``litmus
inspect`` (recorded by ``litmus run`` as ``suite_target``, or named with ``--suite``),
never derived from evidence, and it is loaded only when the tab is first opened so a
saved inspection alone imports nothing. A suite whose declared tree differs from the
saved one is refused before anything runs, because a familiar name on the wrong node is
worse than no bench at all. Execution goes through ``LitmusTest.run(subtree=...)``, so the
framework still owns every configure and cleanup on the path to the node; this is the
sealed lifecycle with the other leaves left pending, not the prohibited
ancestor-configure replay. Evidence lands under a scratch directory: a node's previous
re-run is deleted when it is re-run again, and the whole directory goes when the
Inspector closes.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from litmus.base import LitmusTest, declared_tree
from litmus.inspector_artifacts import ArtifactDiagnostic
from litmus.inspector_run import SavedRun, SavedRunReader
from litmus.inspector_selection import InspectorSelection, SelectionView
from litmus.inspector_tree import TreeRow
from litmus.results import SummaryTreeNode

type SuiteFactory = Callable[[], LitmusTest[Any]]
"""Construct a fresh suite instance; a ``LitmusTest`` runs once, so every re-run needs one.

The command line supplies it. Loading may fail for the same reasons ``litmus run``
refuses a target, and the message of that failure is what the tab shows.
"""

_ARTIFACT_ROOT_VARIABLE = "LITMUS_ARTIFACT_ROOT"
_SCRATCH_PREFIX = "litmus-rerun-"


@dataclass(frozen=True, slots=True)
class RerunRecord:
    """One node's state from the latest re-run that executed it.

    ``source`` is the row the technician pressed Enter on; when that row is re-run
    again, every record it produced is dropped together with its evidence. ``row`` is
    the node's row in that re-run's own tree, which has the same indices as the saved
    tree because the two were matched before the run.
    """

    source: int
    row: TreeRow


class RerunSession:
    """Own the suite adapter, the scratch evidence, and the per-node re-run records.

    Construct with the saved run and either a factory or the reason no suite is
    available. ``prepare`` is awaited when the tab is first opened; ``run`` executes one
    node and is awaited by the screen's worker; ``select`` loads a re-run node's own
    chronology the way the saved selection does; ``close`` removes every byte the bench
    wrote. All methods run on one event loop.
    """

    def __init__(
        self,
        saved: SavedRun,
        factory: SuiteFactory | str,
        *,
        scratch_parent: Path | None = None,
    ) -> None:
        self._saved = saved
        self._factory: SuiteFactory | None = None
        self.unavailable: str | None = None
        if callable(factory):
            self._factory = factory
        else:
            self.unavailable = factory
        self._prepared = self._factory is None
        self._scratch_parent = scratch_parent
        self._root: Path | None = None
        self.running: int | None = None
        self.diagnostic: str | None = None
        self._records: dict[int, RerunRecord] = {}
        self._selections: dict[int, InspectorSelection] = {}
        self._view: SelectionView | None = None

    @property
    def available(self) -> bool:
        """Whether Enter can execute anything: a suite loaded and matched to the saved tree."""
        return self._prepared and self.unavailable is None

    @property
    def scratch_root(self) -> Path | None:
        """Where re-run evidence lives until ``close``; None before the first re-run."""
        return self._root

    @property
    def view(self) -> SelectionView | None:
        """The selected node's re-run chronology, or None when it was never re-run."""
        return self._view

    def record(self, index: int) -> RerunRecord | None:
        """The latest re-run state of the saved row at ``index``, if any run reached it."""
        return self._records.get(index)

    def running_indices(self) -> range:
        """The rows executing now: the pressed node and its descendants, else empty."""
        if self.running is None:
            return range(0)
        return subtree_indices(self._saved.tree.rows, self.running)

    async def prepare(self) -> None:
        """Load the suite once, on first entry to the tab, and hold it to the saved tree.

        The import happens off the event loop because a suite module may do arbitrary
        work at import time. Any failure becomes the tab's reason, worded by the
        loader; a shape mismatch names the first row that differs.
        """
        if self._prepared or self._factory is None:
            return
        self._prepared = True
        try:
            suite = await asyncio.to_thread(self._factory)
            declared = declared_tree(suite)
        except Exception as error:
            self.unavailable = _describe(error)
            return
        self.unavailable = first_mismatch(self._saved.tree.rows, declared)

    async def run(self, index: int, *, on_start: Callable[[], None] | None = None) -> None:
        """Execute the saved row at ``index`` afresh, replacing its previous re-run.

        Evidence for this node goes under ``scratch/<index>/``, emptied first so the
        ninth re-run leaves nothing of the eighth. The recorded environment seeds the
        run so the station's variables travel with the record; the live environment
        overrides it so the technician can change one deliberately. A lifecycle that
        cannot start, or a result that cannot be read back, is reported on the tab, not
        raised through the screen. ``on_start`` is called once the node is marked
        running, before any I/O, so a screen shows it without waiting for a tick.
        """
        if self.running is not None or not self.available or self._factory is None:
            return
        row = self._saved.tree.rows[index]
        self.running = index
        self.diagnostic = None
        self._view = None
        if on_start is not None:
            on_start()
        try:
            node_root = await asyncio.to_thread(self._reset_scratch, index)
            self._forget(index)
            suite = await asyncio.to_thread(self._factory)
            recorded = cast(dict[str, str], self._saved.tree.result.get("env") or {})
            env = {**recorded, **os.environ, _ARTIFACT_ROOT_VARIABLE: str(node_root)}
            await suite.run(env=env, subtree=subtree_of(self._saved.tree.rows, index))
            await self._adopt(index, node_root)
        except Exception as error:
            name = "/".join(row.ancestor_names)
            self.diagnostic = f"re-run of {name} did not settle: {_describe(error)}"
        finally:
            self.running = None

    def interrupt(self) -> None:
        """Stop the live re-run the way an operator at the station would.

        The framework's own operator-signal path is the termination contract: the
        first signal aborts the run with bounded cleanup and an Aborted record, a
        second one during that cleanup exits at once. Raising the signal here, rather
        than cancelling the task, keeps the re-run on exactly that path.
        """
        if self.running is None:
            return
        signal.raise_signal(signal.SIGINT)

    async def select(self, index: int) -> SelectionView | None:
        """Load the re-run chronology of the saved row at ``index``, if a re-run reached it."""
        record = self._records.get(index)
        if record is None:
            self._view = None
            return None
        selection = self._selections[record.source]
        await selection.select(index)
        self._view = selection.view
        return self._view

    def close(self) -> None:
        """Remove the scratch directory; re-run evidence never outlives the Inspector."""
        if self._root is not None:
            shutil.rmtree(self._root, ignore_errors=True)
        self._root = None
        self._records.clear()
        self._selections.clear()
        self._view = None

    def _reset_scratch(self, index: int) -> Path:
        if self._root is None:
            self._root = Path(tempfile.mkdtemp(prefix=_SCRATCH_PREFIX, dir=self._scratch_parent))
        node_root = self._root / str(index)
        shutil.rmtree(node_root, ignore_errors=True)
        node_root.mkdir()
        return node_root

    def _forget(self, source: int) -> None:
        self._records = {
            index: record for index, record in self._records.items() if record.source != source
        }
        self._selections.pop(source, None)

    async def _adopt(self, source: int, node_root: Path) -> None:
        """Read the re-run's result back through the saved-run reader and keep its rows."""
        results = await asyncio.to_thread(lambda: sorted(node_root.rglob("result.json")))
        if len(results) != 1:
            self.diagnostic = f"re-run wrote {len(results)} result.json files under {node_root}"
            return
        rerun = await SavedRunReader().read(results[0])
        if isinstance(rerun, ArtifactDiagnostic):
            self.diagnostic = f"re-run result unreadable: {rerun.message}"
            return
        saved_rows = self._saved.tree.rows
        if len(rerun.tree.rows) != len(saved_rows):
            self.diagnostic = (
                f"re-run recorded {len(rerun.tree.rows)} nodes where the saved run has "
                f"{len(saved_rows)}; the suite changed shape while the Inspector was open"
            )
            return
        self._selections[source] = InspectorSelection(rerun.tree, rerun.run_root)
        for row in rerun.tree.rows:
            if row.state is not None:
                self._records[row.index] = RerunRecord(source, row)


def subtree_of(rows: Sequence[TreeRow], index: int) -> tuple[int, ...]:
    """The child positions from the root that select ``rows[index]`` for ``run(subtree=)``.

    Positions, not names, because two siblings may share a name and the technician
    pressed Enter on one of them.
    """
    path: list[int] = []
    row = rows[index]
    while row.parent_index is not None:
        parent = row.parent_index
        path.append(sum(1 for other in rows[: row.index] if other.parent_index == parent))
        row = rows[parent]
    return tuple(reversed(path))


def subtree_indices(rows: Sequence[TreeRow], index: int) -> range:
    """The preorder range of ``rows[index]`` and its descendants."""
    depth = len(rows[index].ancestor_names)
    end = index + 1
    while end < len(rows) and len(rows[end].ancestor_names) > depth:
        end += 1
    return range(index, end)


def first_mismatch(rows: Sequence[TreeRow], declared: SummaryTreeNode) -> str | None:
    """Why the declared tree is not the saved one, or None when every node lines up.

    Name and type are compared in preorder, the same identity the saved rows carry.
    The first difference is reported by saved path so the technician can see which
    node the supplied suite would have run instead.
    """
    flat: list[tuple[str, str]] = []
    frontier = [declared]
    while frontier:
        node = frontier.pop()
        flat.append((node.name, node.type.value))
        frontier.extend(reversed(node.children))
    for row, (name, node_type) in zip(rows, flat, strict=False):
        saved = (cast(str, row.record["name"]), cast(str, row.record["type"]))
        if saved != (name, node_type):
            path = "/".join(row.ancestor_names)
            return (
                f"the supplied suite differs from the saved run at {path}: saved "
                f"{saved[0]!r} ({saved[1]}), suite {name!r} ({node_type}); reopen with "
                "--suite naming the suite that produced this run"
            )
    if len(rows) != len(flat):
        return (
            f"the supplied suite declares {len(flat)} nodes where the saved run has "
            f"{len(rows)}; reopen with --suite naming the suite that produced this run"
        )
    return None


def _describe(error: BaseException) -> str:
    message = str(error)
    return f"{type(error).__name__}: {message}" if message else type(error).__name__
