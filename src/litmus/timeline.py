"""Durable, per-node records for a run's authoritative chronology."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO, final

_FORMAT_VERSION = 1
_POINTER_FIELDS = (
    "artifact_name",
    "artifact_path",
    "artifact_timestamp",
    "artifact_type",
)
_UNSAFE_COMPONENT = re.compile(r"[^A-Za-z0-9_.-]+")


@dataclass(frozen=True, slots=True)
class TimelinePaths:
    """The two mirrored files owned by one logging node."""

    directory: Path
    human: Path
    json_lines: Path


@dataclass(slots=True)
class _OpenTimeline:
    human: TextIO
    json_lines: TextIO


@final
class RunTimeline:
    """Stream scoped records into durable human and Inspector views."""

    __slots__ = ("_counter", "_lock", "_open", "_root")

    def __init__(self, root: Path) -> None:
        self._root = root
        self._counter = 0
        self._lock = threading.RLock()
        self._open: dict[Path, _OpenTimeline] = {}

    @property
    def root(self) -> Path:
        """The run directory every registered timeline lives beneath."""
        return self._root

    def register(self, scope: tuple[str, ...]) -> TimelinePaths:
        """Reserve a collision-resistant directory without opening any files."""
        if not scope:
            raise ValueError(
                "a timeline owner must have a node scope; pass a non-empty (name, ...) lineage"
            )
        with self._lock:
            self._counter += 1
            ordinal = self._counter
        name = _UNSAFE_COMPONENT.sub("-", scope[-1]).strip(".-") or "node"
        name = name[:48]
        digest = hashlib.sha256("\0".join(scope).encode()).hexdigest()[:12]
        directory = self._root / f"{ordinal:04d}-{name}-{digest}"
        return TimelinePaths(
            directory=directory,
            human=directory / "timeline.log",
            json_lines=directory / "timeline.jsonl",
        )

    def write(
        self,
        paths: TimelinePaths,
        *,
        scope: tuple[str, ...],
        level: int,
        message: str,
        extra: dict[str, Any] | None,
    ) -> None:
        """Append one complete entry to both views and flush it before returning."""
        timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        level_name = logging.getLevelName(level).lower()
        record: dict[str, object] = {
            "format_version": _FORMAT_VERSION,
            "timestamp": timestamp,
            "scope": list(scope),
            "level": level_name,
            "message": message,
        }
        if extra is not None:
            for field in _POINTER_FIELDS:
                if field in extra:
                    record[field] = extra[field]

        human_message = message.replace("\\", "\\\\").replace("\r", "\\r").replace("\n", "\\n")
        human_line = f"{timestamp} [{level_name.upper()}]: {human_message}\n"
        json_line = f"{json.dumps(record, allow_nan=False, ensure_ascii=False, sort_keys=True)}\n"

        with self._lock:
            opened = self._open.get(paths.json_lines)
            if opened is None:
                paths.directory.mkdir(parents=True, exist_ok=True)
                opened = _OpenTimeline(
                    human=paths.human.open("a", encoding="utf-8", newline="\n"),
                    json_lines=paths.json_lines.open("a", encoding="utf-8", newline="\n"),
                )
                self._open[paths.json_lines] = opened
            opened.human.write(human_line)
            opened.human.flush()
            opened.json_lines.write(json_line)
            opened.json_lines.flush()

    def close(self, paths: TimelinePaths) -> None:
        """Close one node's files at its terminal boundary."""
        with self._lock:
            opened = self._open.pop(paths.json_lines, None)
            if opened is None:
                return
            opened.human.close()
            opened.json_lines.close()

    def close_all(self) -> None:
        """Close any handles left by an exceptional framework exit."""
        with self._lock:
            paths = tuple(self._open)
            for json_path in paths:
                opened = self._open.pop(json_path)
                opened.human.close()
                opened.json_lines.close()
