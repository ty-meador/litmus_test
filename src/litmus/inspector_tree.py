"""Project saved result trees into Inspector rows and selection, without running suites.

The result reader owns schema validation; this model owns declaration order, cursor
movement, and recorded log ownership. It neither opens timeline paths nor infers an
artifact root. A future terminal can use the same rows for Inspect and Failures without
recalculating verdicts or reconstructing live framework objects.
"""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from litmus.inspector_artifacts import ArtifactDiagnostic, ArtifactReader
from litmus.run_data import JsonObject

type RecordedState = Literal["Pass", "Fail", "Aborted"] | None


@dataclass(frozen=True, slots=True)
class TreeRow:
    """One recorded node, identified by position even when sibling names are identical.

    ``ancestor_names`` retains complete name components, including embedded slashes.
    ``record`` retains every saved field, including additive evidence and nested causes;
    consumers must treat it as read-only. The owner index is absent for pending nodes.
    """

    index: int
    parent_index: int | None
    ancestor_names: tuple[str, ...]
    record: JsonObject
    log_owner_index: int | None

    @property
    def state(self) -> RecordedState:
        """Return the saved peer state, or None for an unreached node."""
        return cast(RecordedState, self.record["state"])

    @property
    def needs_attention(self) -> bool:
        """Keep Fail and Aborted reachable without collapsing their recovery meanings."""
        return self.state in ("Fail", "Aborted")

    @property
    def not_taken(self) -> tuple[str, ...]:
        """Name absent declared readings in declaration order; null and zero are present."""
        declared = cast(list[str], self.record["evidence_declared"] or [])
        evidence = cast(JsonObject, self.record["evidence"] or {})
        return tuple(key for key in declared if key not in evidence)


class InspectorTree:
    """Own a mutable cursor over a validated, saved root result's fixed preorder rows.

    Construct through ``TreeReader`` at the file boundary. The supplied result must
    already satisfy the version-1 schema. Result and row records are retained intact
    and treated as read-only; cursor operations never edit them or touch the filesystem.
    """

    def __init__(self, result: JsonObject) -> None:
        self.result = result
        self._rows = _rows(cast(JsonObject, result["summary_tree"]))
        self._cursor = 0

    @property
    def rows(self) -> tuple[TreeRow, ...]:
        """All declared nodes in preorder, including pending and composite nodes."""
        return self._rows

    @property
    def failures(self) -> tuple[TreeRow, ...]:
        """The same rows restricted to Fail and Aborted, preserving order and identity."""
        return tuple(row for row in self._rows if row.needs_attention)

    @property
    def selected(self) -> TreeRow:
        """The current row; a schema-valid result always supplies at least its root."""
        return self._rows[self._cursor]

    @property
    def log_owner(self) -> TreeRow | None:
        """The selected node's nearest recorded owner, never a pending node's ancestor.

        An owner names the recorded link, not proof that the file remains available.
        Run-root containment and availability must be checked before a caller opens it.
        """
        index = self.selected.log_owner_index
        return None if index is None else self._rows[index]

    def select(self, index: int) -> None:
        """Select a tree row by index; raise IndexError without moving on invalid input.

        Failure-list rows carry these same indices, so switching views requires no
        lookup by ambiguous display name or reconstruction of a second tree.
        """
        if not 0 <= index < len(self._rows):
            raise IndexError(f"tree row {index} is outside the saved tree")
        self._cursor = index

    def move(self, offset: int) -> None:
        """Move by a signed row count, stopping at the first or last row without wrapping."""
        self._cursor = min(max(0, self._cursor + offset), len(self._rows) - 1)

    def next_failure(self) -> None:
        """Select the next Fail or Aborted row; stay put when none follows."""
        self._jump(range(self._cursor + 1, len(self._rows)))

    def previous_failure(self) -> None:
        """Select the previous Fail or Aborted row; stay put when none precedes."""
        self._jump(range(self._cursor - 1, -1, -1))

    def _jump(self, candidates: range) -> None:
        for index in candidates:
            if self._rows[index].needs_attention:
                self._cursor = index
                return


class TreeReader:
    """Open a selected saved result using the existing versioned result-file boundary."""

    async def read(self, path: Path) -> InspectorTree | ArtifactDiagnostic:
        """Return a tree or an explicit result diagnostic; never manufacture a missing run.

        The selected file's parent is the opened run root. Schema validation, decoding,
        and contained file I/O belong to ``ArtifactReader`` and run in its worker.
        No suite source is discovered, imported, or executed by this reader.
        """
        path = await asyncio.to_thread(path.absolute)
        content = await ArtifactReader(path.parent).read(str(path), kind="result")
        if isinstance(content, ArtifactDiagnostic):
            return content
        return InspectorTree(content.data)


def _rows(root: JsonObject) -> tuple[TreeRow, ...]:
    """Flatten the validated tree, carrying its actual ancestors rather than breadcrumbs."""
    rows: list[TreeRow] = []
    pending: list[tuple[JsonObject, int | None, tuple[str, ...], int | None]] = [
        (root, None, (), None)
    ]
    while pending:
        record, parent, ancestors, owner = pending.pop()
        index = len(rows)
        names = (*ancestors, cast(str, record["name"]))
        if record["state"] is None:
            # A pending row produced no chronology, even if a damaged writer gave
            # it a link. Borrowing an ancestor's log would imply the step executed.
            owner = None
        elif record["log_path"] is not None:
            owner = index
        rows.append(TreeRow(index, parent, names, record, owner))
        children = cast(list[JsonObject], record["children"])
        pending.extend((child, index, names, owner) for child in reversed(children))
    return tuple(rows)
