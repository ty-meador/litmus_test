"""Load and page one saved capture, or two side by side, for the Inspector's artifact modal.

``ArtifactNavigation`` owns which capture is selected and ``ArtifactReader`` owns
reading it; the screen needs lines it can print and one page cursor that moves both
sides of a comparison together (``docs/inspector.md``, "Reading and comparing
artifacts"). This model owns exactly that join. It binds no terminal key, decides no
verdict, and changes no evidence: an unavailable or undecodable capture is shown as
the reader's diagnostic with the recorded reference, never as another file.

A delayed read never replaces a newer selection. Every load carries its own identity,
in the pattern of ``InspectorSelection``, so a screen that moves faster than the disk
still shows the capture it last asked for.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Literal, cast

from litmus.inspector_artifact_navigation import ArtifactNavigation, ArtifactTarget
from litmus.inspector_artifacts import ArtifactContent, ArtifactDiagnostic, ArtifactReader
from litmus.inspector_timeline_view import TimelineView


@dataclass(frozen=True, slots=True)
class ArtifactPane:
    """One side of the modal: the selected target, what the reader returned, and its lines."""

    target: ArtifactTarget
    content: ArtifactContent | ArtifactDiagnostic
    lines: tuple[str, ...]

    @property
    def title(self) -> str:
        """The capture's number, recorded name, type, and timestamp, for the pane header."""
        data = self.target.record.data
        return (
            f"[{self.target.number}] {data['artifact_name']} ({data['artifact_type']}) "
            f"{data['artifact_timestamp']}"
        )

    @property
    def diagnostic(self) -> ArtifactDiagnostic | None:
        """The reader's diagnostic when the capture could not be shown, else ``None``."""
        return self.content if isinstance(self.content, ArtifactDiagnostic) else None


@dataclass(frozen=True, slots=True)
class DiffRow:
    """One aligned row of a comparison; ``None`` on a side that has no line there."""

    left: str | None
    right: str | None
    same: bool


def render_lines(content: ArtifactContent | ArtifactDiagnostic) -> tuple[str, ...]:
    """Turn a read capture into printable lines, or a diagnostic into two.

    A capture is pretty-printed in its recorded key order so that two captures of one
    type line up for comparison. A diagnostic keeps the recorded reference on its own
    line: the Floor Technician needs to see what the run pointed at, not a guess.
    """
    if isinstance(content, ArtifactDiagnostic):
        return (f"{content.kind}: {content.message}", f"recorded reference: {content.reference}")
    return tuple(json.dumps(content.data, indent=2, ensure_ascii=False).splitlines())


def align(left: tuple[str, ...], right: tuple[str, ...]) -> tuple[DiffRow, ...]:
    """Pair two line sequences row by row so the screen can page them together.

    Equal runs pair line for line; a replaced run pairs as far as both sides reach and
    pads the shorter side; an insertion or deletion leaves the other side empty. The
    alignment is what makes "page both sides together" mean something when the two
    captures differ in length.
    """
    rows: list[DiffRow] = []
    matcher = SequenceMatcher(None, left, right, autojunk=False)
    for tag, left_start, left_end, right_start, right_end in matcher.get_opcodes():
        left_run = left[left_start:left_end]
        right_run = right[right_start:right_end]
        if tag == "equal":
            rows.extend(DiffRow(a, b, True) for a, b in zip(left_run, right_run, strict=True))
            continue
        length = max(len(left_run), len(right_run))
        for index in range(length):
            rows.append(
                DiffRow(
                    left_run[index] if index < len(left_run) else None,
                    right_run[index] if index < len(right_run) else None,
                    False,
                )
            )
    return tuple(rows)


class ArtifactModal:
    """Selection, loading, and paging for the artifact modal over one completed chronology.

    Construct one per modal opening with the capture number the operator pressed; the
    navigation model refuses a number the timeline does not carry. ``open`` loads the
    selection; each navigation method steps the selection and reloads. In single mode
    ``rows`` pages the one capture's lines; in diff mode it pages the aligned rows of the
    two captures. Any change of selection returns to the first page, because a capture
    is read from its top, unlike a chronology that opens at its tail.
    """

    def __init__(
        self,
        timeline: TimelineView,
        reader: ArtifactReader,
        *,
        initial_number: int,
        page_size: int,
    ) -> None:
        if page_size < 1:
            raise ValueError("page size must be at least one line")
        self._navigation = ArtifactNavigation(timeline, initial_number=initial_number)
        self._reader = reader
        self._page_size = page_size
        self._page_index = 0
        self._loads = 0
        self._status: Literal["loading", "ready"] = "loading"
        self._left: ArtifactPane | None = None
        self._right: ArtifactPane | None = None
        self._rows: tuple[str, ...] | tuple[DiffRow, ...] = ()

    @property
    def navigation(self) -> ArtifactNavigation:
        """The selection model, for the screen's type and capture indicators."""
        return self._navigation

    @property
    def diff(self) -> bool:
        """Whether two captures are being compared."""
        return self._navigation.comparison is not None

    @property
    def status(self) -> Literal["loading", "ready"]:
        """``loading`` until the current selection's read has landed."""
        return self._status

    @property
    def left(self) -> ArtifactPane | None:
        """The single capture, or the left side of a comparison, once loaded."""
        return self._left

    @property
    def right(self) -> ArtifactPane | None:
        """The right side of a comparison; ``None`` in single mode or while loading."""
        return self._right

    @property
    def page_size(self) -> int:
        return self._page_size

    @property
    def page_index(self) -> int:
        return self._page_index

    @property
    def page_count(self) -> int:
        """At least one page, so an empty or unloaded pane still has somewhere to be."""
        return max(1, -(-len(self._rows) // self._page_size))

    @property
    def rows(self) -> tuple[str, ...] | tuple[DiffRow, ...]:
        """The current page: lines in single mode, aligned rows in diff mode."""
        start = self._page_index * self._page_size
        return self._rows[start : start + self._page_size]

    async def open(self) -> None:
        """Load the capture the modal was opened on."""
        await self._load()

    async def next_type(self) -> None:
        self._navigation.next_type()
        await self._load()

    async def previous_type(self) -> None:
        self._navigation.previous_type()
        await self._load()

    async def next_artifact(self) -> None:
        self._navigation.next_artifact()
        await self._load()

    async def previous_artifact(self) -> None:
        self._navigation.previous_artifact()
        await self._load()

    async def toggle_diff(self) -> None:
        """Enter or leave comparison; a singleton type stays single without a reload."""
        was_diff = self.diff
        self._navigation.toggle_diff()
        if self.diff != was_diff:
            await self._load()

    def next_page(self) -> None:
        self._page_index = min(self._page_index + 1, self.page_count - 1)

    def previous_page(self) -> None:
        self._page_index = max(self._page_index - 1, 0)

    def resize(self, page_size: int) -> None:
        """Keep the first visible row in view when the terminal changes height."""
        if page_size < 1:
            raise ValueError("page size must be at least one line")
        first_visible = self._page_index * self._page_size
        self._page_size = page_size
        self._page_index = min(first_visible // page_size, self.page_count - 1)

    async def _load(self) -> None:
        """Read the current selection, applying it only if no newer selection followed."""
        self._loads += 1
        identity = self._loads
        self._status = "loading"
        self._left = None
        self._right = None
        self._rows = ()
        self._page_index = 0
        comparison = self._navigation.comparison
        targets = comparison if comparison is not None else (self._navigation.current,)
        panes = [await self._pane(target) for target in targets]
        if identity != self._loads:
            return
        self._left = panes[0]
        if comparison is None:
            self._rows = panes[0].lines
        else:
            self._right = panes[1]
            self._rows = align(panes[0].lines, panes[1].lines)
        self._status = "ready"

    async def _pane(self, target: ArtifactTarget) -> ArtifactPane:
        content = await self._reader.read(target.reference, kind=target.kind)
        return ArtifactPane(target, content, render_lines(content))


def single_rows(modal: ArtifactModal) -> tuple[str, ...]:
    """The current page as lines; only meaningful in single mode."""
    if modal.diff:
        raise ValueError("the modal is comparing two captures; use diff_rows")
    return cast("tuple[str, ...]", modal.rows)


def diff_rows(modal: ArtifactModal) -> tuple[DiffRow, ...]:
    """The current page as aligned rows; only meaningful in diff mode."""
    if not modal.diff:
        raise ValueError("the modal is showing one capture; use single_rows")
    return cast("tuple[DiffRow, ...]", modal.rows)
