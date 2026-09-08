"""Read saved timelines for the Inspector without executing suites or changing evidence.

This is the temporal reader boundary, independent of tree navigation and rendering.
Diagnostics occupy their physical position beside valid records, so a future severity
filter can keep damage visible without treating it as a logged hardware failure.
"""

import asyncio
import json
from dataclasses import dataclass
from importlib.metadata import distribution
from pathlib import Path
from typing import Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from litmus.inspector_json import JsonDecoder
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
    """A file's readable records and diagnostics in physical order, with its source path."""

    path: Path
    rows: tuple[TimelineRecord | TimelineDiagnostic, ...]


class TimelineReader:
    """Read version-1 chronology using the schema shipped with this installation.

    This internal Inspector component accepts an already selected timeline path. Tree
    ownership, run-root containment, artifact resolution, and cursor state belong to
    the caller; the reader follows no pointer and imports no suite source.
    """

    async def read(self, path: Path) -> TimelineRead:
        """Return every valid record with explicit damage and availability diagnostics.

        File I/O and schema validation run at a worker boundary so loading a rack log
        does not block terminal input. Caller cancellation propagates; the read-only
        worker may finish independently, but holds no UI state to overwrite. Packaging
        or schema defects raise rather than masquerading as damage to operator evidence.
        """
        return await asyncio.to_thread(self._read, path)

    def _read(self, path: Path) -> TimelineRead:
        # Build configuration packages the published file verbatim. Distribution data
        # also resolves in editable installs, where litmus itself lives in src/.
        schema_path = distribution("litmus").locate_file("litmus_data/timeline-v1.schema.json")
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        validator = Draft202012Validator(schema)
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
