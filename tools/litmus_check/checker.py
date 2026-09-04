"""Run a rule registry over suite files and directories."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from litmus_check.model import SuiteModule, Violation
from litmus_check.registry import RuleRegistry


@dataclass(frozen=True, slots=True)
class Report:
    """What a check produced.

    ``violations`` are rule findings, sorted by location. ``errors`` are files the
    checker could not analyse at all — unreadable or not valid Python — each rendered
    as ``path: reason``. A report with errors is never clean: a suite that cannot be
    parsed has not been checked.
    """

    violations: tuple[Violation, ...] = field(default_factory=tuple)
    errors: tuple[str, ...] = field(default_factory=tuple)

    @property
    def clean(self) -> bool:
        """True when nothing was found and nothing failed to parse."""
        return not self.violations and not self.errors


class SuiteChecker:
    """Apply every rule in a registry to every ``.py`` file under the given paths.

    Directories are walked recursively; ``__pycache__`` and dot-directories are
    skipped. A path that is neither a file nor a directory is reported as an error,
    not ignored, because a CI gate pointed at a renamed directory must fail loudly.
    """

    def __init__(self, registry: RuleRegistry) -> None:
        self._registry = registry

    def check(self, paths: Iterable[Path]) -> Report:
        """Check every file reachable from ``paths``."""
        violations: list[Violation] = []
        errors: list[str] = []
        for path in paths:
            if not path.exists():
                errors.append(f"{path}: no such file or directory")
                continue
            for file in self._python_files(path):
                try:
                    module = SuiteModule(file, file.read_text(encoding="utf-8"))
                except (OSError, SyntaxError, UnicodeDecodeError) as exc:
                    errors.append(f"{file}: {exc}")
                    continue
                for rule in self._registry:
                    violations.extend(rule.check(module))
        return Report(tuple(sorted(violations)), tuple(errors))

    @staticmethod
    def _python_files(path: Path) -> Iterator[Path]:
        if path.is_file():
            yield path
            return
        for candidate in sorted(path.rglob("*.py")):
            relative = candidate.relative_to(path)
            skipped = any(part == "__pycache__" or part.startswith(".") for part in relative.parts)
            if not skipped:
                yield candidate
