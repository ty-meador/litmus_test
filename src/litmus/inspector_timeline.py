"""Read saved timelines for the Inspector without executing suites or changing evidence.

This is the temporal reader boundary, independent of tree navigation and rendering.
Diagnostics occupy their physical position beside valid records, so a future severity
filter can keep damage visible without treating it as a logged hardware failure.
"""

import asyncio
import json
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from litmus.inspector_json import JsonDecoder, RecordSchema
from litmus.run_data import JsonObject


@dataclass(frozen=True, slots=True)
class TimelineRecord:
    """One schema-validated object, retaining additive fields and its physical line number."""

    line_number: int
    data: JsonObject


@dataclass(frozen=True, slots=True)
class TimelineDiagnostic:
    """Unreadable evidence, distinct from log severity and from the run's verdict.

    A file-level I/O error has no line number. Every decoding diagnostic names the
    one-based physical line it replaces; subsequent complete records remain readable.
    """

    line_number: int | None
    kind: Literal["truncated", "invalid-record", "unsupported-version", "unavailable"]
    message: str


@dataclass(frozen=True, slots=True)
class TimelineRead:
    """Chronology with its source path and, when supplied, the recorded relative reference.

    A reference that cannot resolve within the opened run has no source path. Its
    unavailable diagnostic still retains the reference so a pane can name the missing
    evidence without substituting another file.
    """

    path: Path | None
    rows: tuple[TimelineRecord | TimelineDiagnostic, ...]
    reference: str | None = None


class TimelineReader:
    """Read version-1 chronology using the schema shipped with this installation.

    Use read_reference for a recorded run-relative log_path, or read for a path already
    selected by a trusted caller. Tree ownership, artifact resolution, and cursor state
    belong to the caller; the reader follows no artifact pointer and imports no source.
    """

    async def read_reference(self, run_root: Path, reference: str) -> TimelineRead:
        """Read a complete POSIX relative path only beneath the opened run directory.

        Absolute, empty, traversing, escaping, missing and non-file targets return an
        unavailable diagnostic. Resolution and reading share one worker boundary.
        The original reference survives relocation and diagnostics; filenames are never
        searched or reduced to basenames. Caller cancellation propagates as for read.
        The operator's run_root may use symlinks or '..'; recorded references may not
        traverse, and their resolved targets must remain inside that root.
        """
        return await asyncio.to_thread(self._read_reference, run_root, reference)

    def _read_reference(self, run_root: Path, reference: str) -> TimelineRead:
        try:
            relative = PurePosixPath(reference)
            if relative.is_absolute() or not relative.parts or ".." in relative.parts:
                raise ValueError("log reference must be nonempty, relative and contain no '..'")
            root = run_root.resolve(strict=True)
            path = root.joinpath(*relative.parts).resolve(strict=True)
            # A lexical prefix check alone admits symlinks into another run's evidence.
            if not path.is_relative_to(root):
                raise ValueError("resolved timeline escapes the opened run root")
            if not path.is_file():
                raise ValueError("timeline is not a regular file")
        except (OSError, ValueError) as error:
            diagnostic = TimelineDiagnostic(
                None, "unavailable", f"log unavailable: {reference}: {error}"
            )
            return TimelineRead(None, (diagnostic,), reference)
        return replace(self._read(path), reference=reference)

    async def read(self, path: Path) -> TimelineRead:
        """Return every valid record with explicit damage and availability diagnostics.

        File I/O and schema validation run at a worker boundary so loading a rack log
        does not block terminal input. Caller cancellation propagates; the read-only
        worker may finish independently, but holds no UI state to overwrite. Packaging
        or schema defects raise rather than masquerading as damage to operator evidence.
        """
        return await asyncio.to_thread(self._read, path)

    def _read(self, path: Path) -> TimelineRead:
        validator = RecordSchema("timeline").validator()
        rows: list[TimelineRecord | TimelineDiagnostic] = []
        try:
            # Decode bytes per physical line: a cut UTF-8 sequence in the tail must
            # not make a text-stream decoder discard complete preceding records.
            with path.open("rb") as stream:
                for number, line in enumerate(stream, 1):
                    rows.append(self._decode(line, number, validator))
        except OSError as error:
            rows.append(
                TimelineDiagnostic(None, "unavailable", f"log unavailable: {path}: {error}")
            )
        return TimelineRead(path, tuple(rows))

    def _decode(
        self, line: bytes, number: int, validator: Draft202012Validator
    ) -> TimelineRecord | TimelineDiagnostic:
        try:
            data = JsonDecoder().decode(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            if not line.endswith(b"\n"):
                return TimelineDiagnostic(
                    number, "truncated", "incomplete final line; log truncated"
                )
            return TimelineDiagnostic(number, "invalid-record", str(error))
        except (ValueError, RecursionError) as error:
            return TimelineDiagnostic(number, "invalid-record", str(error))

        if not isinstance(data, dict):
            return TimelineDiagnostic(number, "invalid-record", "timeline record must be an object")
        version = data.get("format_version", "missing")
        if type(version) is not int or version != 1:
            return TimelineDiagnostic(
                number,
                "unsupported-version",
                f"timeline format_version {version!r}; reader accepts integer 1",
            )
        try:
            # jsonschema's validate method is untyped; runtime validation, rather
            # than a cast of the input, earns the record's accepted status here.
            validator.validate(data)  # pyright: ignore[reportUnknownMemberType]
        except ValidationError as error:
            return TimelineDiagnostic(number, "invalid-record", error.message)
        return TimelineRecord(number, data)
