"""Present one loaded Inspector chronology without reinterpreting its evidence.

The selection model owns loading and stale-read rejection. This synchronous view owns
only paging, severity filtering, and capture numbers within a completed TimelineRead.
A terminal renders the retained records and diagnostics; artifact I/O stays with the
saved run's contained capture reader, and no view operation changes a recorded verdict.
"""

from dataclasses import dataclass
from typing import cast

from litmus.inspector_timeline import TimelineDiagnostic, TimelineRead, TimelineRecord


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    """A recorded line and its optional one-based capture number, never written into JSON.

    Keep the complete record so the renderer cannot lose physical line numbers, scopes,
    timestamps, or additive fields while paging. A number identifies a structured
    pointer in the complete chronology, not its position on the current page.
    """

    record: TimelineRecord
    artifact_number: int | None


class TimelineView:
    """Mutable presentation of one validated timeline; evidence is treated as read-only.

    Construct a fresh view for each completed selection load, including a reload of
    the same file. It opens at the tail with filtering off, as the legacy log pane did.
    Page size counts records or diagnostics, not wrapped terminal lines; the renderer
    supplies a positive integer after reserving space for its header and controls.
    """

    def __init__(self, timeline: TimelineRead, *, page_size: int) -> None:
        self._page_size = _page_size(page_size)
        self._timeline = timeline
        rows: list[TimelineEntry | TimelineDiagnostic] = []
        artifacts: list[TimelineRecord] = []
        for row in timeline.rows:
            if isinstance(row, TimelineDiagnostic):
                rows.append(row)
                continue
            number = None
            # The reader's schema guarantees the pointer fields travel together.
            # Even an empty path stays numbered: the capture reader must diagnose it.
            if "artifact_path" in row.data:
                artifacts.append(row)
                number = len(artifacts)
            rows.append(TimelineEntry(row, number))
        self._all_rows = tuple(rows)
        self._artifacts = tuple(artifacts)
        self._visible = self._all_rows
        self._errors_only = False
        self._page_index = self.page_count - 1

    @property
    def timeline(self) -> TimelineRead:
        """The source read, retaining its recorded reference and resolved path."""
        return self._timeline

    @property
    def rows(self) -> tuple[TimelineEntry | TimelineDiagnostic, ...]:
        """Current page in physical order; diagnostics keep their original positions."""
        start = self._page_index * self._page_size
        return self._visible[start : start + self._page_size]

    @property
    def page_index(self) -> int:
        """Zero-based current page, always within the current projection."""
        return self._page_index

    @property
    def page_count(self) -> int:
        """Number of pages; an empty projection still has one empty page."""
        return max(1, (len(self._visible) + self._page_size - 1) // self._page_size)

    @property
    def errors_only(self) -> bool:
        """Whether ordinary records are restricted to Python error and critical levels."""
        return self._errors_only

    @property
    def artifact_count(self) -> int:
        """Count every structured pointer, including those outside the current page/filter."""
        return len(self._artifacts)

    def artifact(self, number: int) -> TimelineRecord | None:
        """Resolve a one-based capture number independently of paging or filtering.

        Missing numbers return None; paths in message text never become pointers.
        Numbers above nine remain addressable for modal navigation, though the legacy
        direct keyboard shortcuts cover only 1-9. Pass the retained artifact_path to
        the saved run's ArtifactReader, using kind='result' for a run-result pointer.
        """
        if type(number) is not int or not 1 <= number <= len(self._artifacts):
            return None
        return self._artifacts[number - 1]

    def next_page(self) -> None:
        """Advance one page, staying at the tail when no next page exists."""
        self._page_index = min(self._page_index + 1, self.page_count - 1)

    def previous_page(self) -> None:
        """Return one page, staying at the beginning rather than wrapping."""
        self._page_index = max(self._page_index - 1, 0)

    def toggle_errors(self) -> None:
        """Toggle severity filtering and return to page zero without renumbering captures.

        Reader diagnostics are evidence about missing or damaged chronology, so they
        survive both projections. Unknown custom levels have no established severity
        and remain available in the full view; they never acquire an invented verdict.
        """
        self._errors_only = not self._errors_only
        self._visible = self._all_rows
        if self._errors_only:
            self._visible = tuple(
                row
                for row in self._all_rows
                if isinstance(row, TimelineDiagnostic)
                or cast(str, row.record.data["level"]).lower() in {"error", "critical"}
            )
        self._page_index = 0

    def resize(self, page_size: int) -> None:
        """Set records per page and clamp the current page; reject invalid sizes unchanged."""
        self._page_size = _page_size(page_size)
        self._page_index = min(self._page_index, self.page_count - 1)


def _page_size(value: int) -> int:
    """Refuse booleans and nonpositive/noninteger sizes before pagination can divide."""
    if type(value) is not int or value < 1:
        raise ValueError("page size must be a positive integer")
    return value
