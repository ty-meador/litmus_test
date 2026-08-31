"""Immutable artifact values and framework-owned persistence."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import final

_ARTIFACT_FORMAT_VERSION = 1
_VALID_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")


def _utc_text(value: datetime) -> str:
    """Render one aware timestamp in stable UTC form."""
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@final
@dataclass(frozen=True, slots=True)
class Artifact:
    """Describe point-in-time evidence without exposing a file-writing operation.

    Persistence belongs exclusively to :meth:`TestUtils.capture_artifact`; keeping this
    value immutable and write-free makes it impossible for the public framework surface
    to create an artifact that lacks its chronological log pointer.
    """

    name: str
    type: str
    data: object
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        """Reject unsafe identities and timestamps that cannot prove chronology."""
        for label, value in (("artifact name", self.name), ("artifact type", self.type)):
            if _VALID_IDENTITY.fullmatch(value) is None:
                detail = "must use only letters, numbers, dashes, and underscores"
                raise ValueError(f"{label} {value!r} {detail}")
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("artifact timestamp must be timezone-aware")
        object.__setattr__(self, "timestamp", self.timestamp.astimezone(UTC))

    def to_json(self) -> dict[str, object]:
        """Return the versioned JSON shape persisted by the framework."""
        return {
            "data": self.data,
            "format_version": _ARTIFACT_FORMAT_VERSION,
            "name": self.name,
            "timestamp": _utc_text(self.timestamp),
            "type": self.type,
        }
