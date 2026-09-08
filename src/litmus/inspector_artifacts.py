"""Read captures beneath an opened run for the Inspector, preserving their recorded references.

Relocation is an explicit mapping, never a search. An unavailable capture cannot borrow
another unit's evidence merely because that file has the same name or still exists at
the original station. This internal boundary owns reading and decoding, not root discovery,
pointer numbering, modal state, or any hardware execution.
"""

import asyncio
import json
from dataclasses import dataclass
from functools import cache
from importlib.metadata import distribution
from pathlib import Path, PurePosixPath
from typing import Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from litmus.inspector_json import JsonDecoder
from litmus.run_data import JsonObject

type RecordKind = Literal["artifact", "result"]


@dataclass(frozen=True, slots=True)
class ArtifactContent:
    """A validated saved object with its original reference and the file actually read."""

    reference: str
    path: Path
    data: JsonObject


@dataclass(frozen=True, slots=True)
class ArtifactDiagnostic:
    """Unavailable or undecodable evidence, never a replacement for a recorded verdict."""

    reference: str
    kind: Literal["unavailable", "unsupported-version", "invalid-record"]
    message: str


@dataclass(frozen=True, slots=True)
class ArtifactReader:
    """Read version-1 captures within one opened run, optionally relocated from a known root.

    ``recorded_root`` is an established original run root supplied by the saved-run
    loader, not inferred here from filenames or timestamps. Without it, only absolute
    references already beneath ``run_root`` resolve. An explicit mapping is authoritative:
    the reader never falls back to the original path when the moved file is absent.
    """

    run_root: Path
    recorded_root: PurePosixPath | None = None

    def __post_init__(self) -> None:
        """Reject a relative or traversing recorded root before accepting a mapping."""
        if self.recorded_root is not None and (
            not self.recorded_root.is_absolute() or ".." in self.recorded_root.parts
        ):
            raise ValueError("recorded root must be absolute and contain no '..' components")

    async def read(
        self, reference: str, *, kind: RecordKind = "artifact"
    ) -> ArtifactContent | ArtifactDiagnostic:
        """Read one pointer with explicit availability, decoding, and version diagnostics.

        Select ``result`` for the root run-result pointer and ``artifact`` for a capture.
        Both use their published schema, accepting additive fields. Filesystem I/O and
        validation stay in a worker; caller cancellation propagates without changing UI
        state or evidence. Installation/schema defects raise as framework defects.
        """
        return await asyncio.to_thread(self._read, reference, kind)

    def _read(self, reference: str, kind: RecordKind) -> ArtifactContent | ArtifactDiagnostic:
        validator = _validator(kind)
        try:
            root = self.run_root.resolve(strict=True)
            recorded = PurePosixPath(reference)
            if not recorded.is_absolute() or ".." in recorded.parts:
                raise ValueError("reference must be absolute and contain no '..' components")
            origin = self.recorded_root if self.recorded_root is not None else PurePosixPath(root)
            tail = recorded.relative_to(origin)
            path = root.joinpath(*tail.parts).resolve(strict=True)
            # Lexical containment alone admits a symlink to a different run's capture.
            if not path.is_relative_to(root):
                raise ValueError("resolved capture escapes the opened run root")
            if not path.is_file():
                raise ValueError("capture is not a regular file")
            raw = path.read_bytes()
        except (OSError, ValueError) as error:
            return ArtifactDiagnostic(
                reference, "unavailable", f"artifact unavailable: {reference}: {error}"
            )

        try:
            data = JsonDecoder().decode(raw)
        except (ValueError, RecursionError) as error:
            return ArtifactDiagnostic(reference, "invalid-record", str(error))
        if not isinstance(data, dict):
            return ArtifactDiagnostic(reference, "invalid-record", f"{kind} must be an object")
        version = data.get("format_version", "missing")
        if type(version) is not int or version != 1:
            return ArtifactDiagnostic(
                reference,
                "unsupported-version",
                f"{kind} format_version {version!r}; reader accepts integer 1",
            )
        try:
            validator.validate(data)  # pyright: ignore[reportUnknownMemberType]
        except ValidationError as error:
            # The full exception pretty-prints the payload; deeply nested damaged
            # evidence can exhaust recursion while formatting its own diagnostic.
            return ArtifactDiagnostic(reference, "invalid-record", error.message)
        except RecursionError as error:
            return ArtifactDiagnostic(reference, "invalid-record", str(error))
        return ArtifactContent(reference, path, data)


@cache
def _validator(kind: RecordKind) -> Draft202012Validator:
    """Compile each packaged contract once, in the worker, rather than per modal selection."""
    path = distribution("litmus").locate_file(f"litmus_data/{kind}-v1.schema.json")
    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)
