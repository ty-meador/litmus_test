"""Select saved captures and comparison pairs for the Inspector's artifact modal.

TimelineView owns pointer identity and numbering; this synchronous model owns only
type grouping and selection. It returns recorded targets to the saved run's contained
ArtifactReader. Loading, content paging, diff rendering, and terminal keys belong to
the caller; navigation never reads a file, changes evidence, or calculates a verdict.
"""

from dataclasses import dataclass
from typing import cast

from litmus.inspector_artifacts import RecordKind
from litmus.inspector_timeline import TimelineRecord
from litmus.inspector_timeline_view import TimelineView


@dataclass(frozen=True, slots=True)
class ArtifactTarget:
    """One numbered pointer, retaining its full record for names, timestamps, and scope."""

    number: int
    record: TimelineRecord

    @property
    def reference(self) -> str:
        """Recorded path, including an empty one that the capture reader must diagnose."""
        return cast(str, self.record.data["artifact_path"])

    @property
    def kind(self) -> RecordKind:
        """Select the result schema only for a structured run-result pointer."""
        return "result" if self.record.data["artifact_type"] == "run-result" else "artifact"


class ArtifactNavigation:
    """Mutable selection within one completed chronology, treated as read-only evidence.

    Types follow first appearance, and captures within each type keep timeline order.
    Construct a new model when opening a modal; initial_number must identify an actual
    pointer. An invalid number raises ValueError instead of displaying different evidence.
    Single mode clamps capture movement and wraps type movement. Diff mode wraps both
    sides within the current type and swaps on collision, keeping the targets distinct.
    """

    def __init__(self, timeline: TimelineView, *, initial_number: int) -> None:
        if timeline.artifact(initial_number) is None:
            raise ValueError("capture number must identify a pointer in the loaded timeline")
        groups: dict[str, list[ArtifactTarget]] = {}
        for number, record in enumerate(timeline.artifacts, 1):
            kind = cast(str, record.data["artifact_type"])
            groups.setdefault(kind, []).append(ArtifactTarget(number, record))
        self._types = tuple(groups)
        self._groups = tuple(tuple(targets) for targets in groups.values())
        self._type_index, self._item_index = next(
            (type_index, item_index)
            for type_index, targets in enumerate(self._groups)
            for item_index, target in enumerate(targets)
            if target.number == initial_number
        )
        self._diff = False
        self._left = 0
        self._right = 0

    @property
    def types(self) -> tuple[str, ...]:
        """Recorded artifact types in first-appearance order, without normalization."""
        return self._types

    @property
    def artifact_type(self) -> str:
        """The selected group; diff movement never changes this type."""
        return self._types[self._type_index]

    @property
    def numbers(self) -> tuple[int, ...]:
        """Stable timeline numbers in the current group, including captures above nine."""
        return tuple(target.number for target in self._groups[self._type_index])

    @property
    def current(self) -> ArtifactTarget:
        """Single-view selection, retained while diff mode selects its own two sides."""
        return self._groups[self._type_index][self._item_index]

    @property
    def comparison(self) -> tuple[ArtifactTarget, ArtifactTarget] | None:
        """Left/right targets of one type, or None in single mode; never the same capture."""
        if not self._diff:
            return None
        group = self._groups[self._type_index]
        return group[self._left], group[self._right]

    def toggle_diff(self) -> None:
        """Compare the current capture with the first other capture, or leave diff.

        A singleton type stays in single mode. Leaving diff restores the original
        single-view selection; entering again starts a fresh pair from that selection.
        """
        if self._diff:
            self._diff = False
        elif len(self._groups[self._type_index]) >= 2:
            self._diff = True
            self._left = self._item_index
            self._right = 1 if self._item_index == 0 else 0

    def next_type(self) -> None:
        """Single: wrap to the next type's first capture. Diff: cycle the left target."""
        self._step_type(1)

    def previous_type(self) -> None:
        """Single: wrap to the previous type's first capture. Diff: cycle the left target."""
        self._step_type(-1)

    def next_artifact(self) -> None:
        """Single: advance within this type, clamping at the end. Diff: cycle right."""
        self._step_artifact(1)

    def previous_artifact(self) -> None:
        """Single: retreat within this type, clamping at the start. Diff: cycle right."""
        self._step_artifact(-1)

    def _step_type(self, delta: int) -> None:
        if self._diff:
            candidate = (self._left + delta) % len(self._groups[self._type_index])
            if candidate == self._right:
                self._left, self._right = self._right, self._left
            else:
                self._left = candidate
        else:
            self._type_index = (self._type_index + delta) % len(self._types)
            self._item_index = 0

    def _step_artifact(self, delta: int) -> None:
        count = len(self._groups[self._type_index])
        if self._diff:
            candidate = (self._right + delta) % count
            if candidate == self._left:
                self._left, self._right = self._right, self._left
            else:
                self._right = candidate
        else:
            self._item_index = max(0, min(self._item_index + delta, count - 1))
