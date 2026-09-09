"""Join a saved Inspector tree to the publication evidence that permits relocation.

The root timeline's final run-result pointer records where this run was published.
This loader establishes that original root for the contained capture reader; it never
searches the station or infers a mapping from directory names. Missing provenance leaves
the tree and chronology available, with an explicit diagnostic instead of guessed evidence.
"""

import asyncio
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import cast

from litmus.inspector_artifacts import ArtifactDiagnostic, ArtifactReader
from litmus.inspector_timeline import (
    TimelineDiagnostic,
    TimelineRead,
    TimelineReader,
    TimelineRecord,
)
from litmus.inspector_tree import InspectorTree


@dataclass(frozen=True, slots=True)
class OriginDiagnostic:
    """Why no original root was established, retaining the root's recorded log reference."""

    reference: str | None
    message: str


@dataclass(frozen=True, slots=True)
class SavedRun:
    """One validated saved result and its contained evidence readers, without live suites.

    The tree owns its navigation cursor. The origin and root chronology describe this
    opening only; reopening reads the evidence afresh. Without a known origin, captures
    already beneath the opened root remain readable, but relocation is unavailable.
    """

    run_root: Path
    tree: InspectorTree
    root_timeline: TimelineRead | None
    origin: PurePosixPath | OriginDiagnostic
    artifacts: ArtifactReader


class SavedRunReader:
    """Open saved evidence and establish relocation only from root publication records."""

    async def read(self, path: Path) -> SavedRun | ArtifactDiagnostic:
        """Validate a selected result before reading its contained root timeline.

        Operator selections may use symlinks or '..'; recorded references may not.
        An unreadable result returns its diagnostic. A missing, damaged, ambiguous or
        misplaced publication keeps the saved tree and chronology with an origin
        diagnostic. I/O uses worker boundaries; cancellation and reader defects propagate.
        No suite code is discovered or executed, and no saved file is changed.
        """
        reference = str(path)
        try:
            path = await asyncio.to_thread(path.resolve)
        except (OSError, ValueError) as error:
            return ArtifactDiagnostic(reference, "unavailable", f"result unavailable: {error}")
        content = await ArtifactReader(path.parent).read(str(path), kind="result")
        if isinstance(content, ArtifactDiagnostic):
            return content
        tree = InspectorTree(content.data)
        link = cast(str | None, tree.rows[0].record["log_path"])
        timeline = None
        origin: PurePosixPath | OriginDiagnostic = OriginDiagnostic(
            link, "original run root unavailable: root has no recorded timeline"
        )
        if link is not None:
            timeline = await TimelineReader().read_reference(path.parent, link)
            origin = _origin(tree, timeline)
        recorded_root = origin if isinstance(origin, PurePosixPath) else None
        return SavedRun(
            path.parent, tree, timeline, origin, ArtifactReader(path.parent, recorded_root)
        )


def _origin(tree: InspectorTree, timeline: TimelineRead) -> PurePosixPath | OriginDiagnostic:
    """Require one final root publication; damage cannot conceal a conflicting pointer."""
    unavailable = OriginDiagnostic(
        timeline.reference,
        "original run root unavailable: expected one final root-scoped result publication "
        "in an undamaged timeline",
    )
    if not timeline.rows or any(isinstance(row, TimelineDiagnostic) for row in timeline.rows):
        return unavailable
    scope = list(tree.rows[0].ancestor_names)
    if tree.result["ancestor_names"] != scope:
        return unavailable
    publications = [
        row
        for row in timeline.rows
        if isinstance(row, TimelineRecord)
        and row.data["scope"] == scope
        and row.data.get("artifact_type") == "run-result"
        and row.data.get("artifact_name") == "result"
    ]
    if len(publications) != 1 or publications[0] is not timeline.rows[-1]:
        return unavailable
    reference = cast(str, publications[0].data["artifact_path"])
    recorded = PurePosixPath(reference)
    if (
        not recorded.is_absolute()
        or ".." in recorded.parts
        or "\x00" in reference
        or recorded.name != "result.json"
    ):
        return OriginDiagnostic(
            timeline.reference,
            f"original run root unavailable: invalid result publication path {reference!r}",
        )
    return recorded.parent
