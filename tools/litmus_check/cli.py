"""The ``litmus-check`` command.

Exit status is the CI contract:

- ``0`` — every checked file is clean.
- ``1`` — at least one rule violation.
- ``2`` — a file could not be checked (missing, unreadable, invalid Python) or the
  invocation itself was wrong (unknown rule code, no paths).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from litmus_check.checker import Report, SuiteChecker
from litmus_check.registry import RuleRegistry

EXIT_CLEAN = 0
EXIT_VIOLATIONS = 1
EXIT_ERROR = 2


class LitmusCheckCommand:
    """Parse arguments, run the checker, and render the report.

    The command holds no state between runs; construct it with the streams to write
    to and call :meth:`run` with the argument vector. Tests drive it exactly the way
    the console script does.
    """

    def __init__(self, stdout: TextIO | None = None, stderr: TextIO | None = None) -> None:
        self._stdout = stdout if stdout is not None else sys.stdout
        self._stderr = stderr if stderr is not None else sys.stderr

    def run(self, argv: Sequence[str]) -> int:
        """Execute one invocation and return its exit status."""
        parser = self._parser()
        args = parser.parse_args(argv)
        registry = RuleRegistry.discover()
        if args.list_rules:
            self._list_rules(registry)
            return EXIT_CLEAN
        if not args.paths:
            parser.print_usage(self._stderr)
            self._stderr.write("litmus-check: error: at least one path is required\n")
            return EXIT_ERROR
        try:
            if args.select:
                registry = registry.select(args.select)
            if args.ignore:
                registry = registry.ignore(args.ignore)
        except KeyError as exc:
            self._stderr.write(f"litmus-check: error: {exc.args[0]}\n")
            return EXIT_ERROR
        report = SuiteChecker(registry).check(Path(p) for p in args.paths)
        self._render(report, args.format)
        if report.errors:
            return EXIT_ERROR
        if report.violations:
            return EXIT_VIOLATIONS
        return EXIT_CLEAN

    @staticmethod
    def _parser() -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(
            prog="litmus-check",
            description=(
                "Tier-3 static analysis for Litmus suites. Skippable by design: tiers 1 "
                "and 2 guard hardware and verdict integrity at runtime and import time; "
                "this tool guards readability and idiom."
            ),
        )
        parser.add_argument("paths", nargs="*", help="suite files or directories to check")
        parser.add_argument(
            "--select", action="append", metavar="CODE", help="run only this rule (repeatable)"
        )
        parser.add_argument(
            "--ignore", action="append", metavar="CODE", help="skip this rule (repeatable)"
        )
        parser.add_argument(
            "--format", choices=("text", "json"), default="text", help="report format"
        )
        parser.add_argument(
            "--list-rules", action="store_true", help="print the rule catalogue and exit"
        )
        return parser

    def _list_rules(self, registry: RuleRegistry) -> None:
        for rule in registry:
            self._stdout.write(f"{rule.code}\t{rule.summary}\n")

    def _render(self, report: Report, fmt: str) -> None:
        if fmt == "json":
            payload = {
                "violations": [
                    {
                        "path": str(v.path),
                        "line": v.line,
                        "column": v.column,
                        "code": v.code,
                        "message": v.message,
                    }
                    for v in report.violations
                ],
                "errors": list(report.errors),
            }
            self._stdout.write(json.dumps(payload, indent=2) + "\n")
            return
        for violation in report.violations:
            self._stdout.write(violation.render() + "\n")
        for error in report.errors:
            self._stderr.write(f"litmus-check: error: {error}\n")
        count = len(report.violations)
        noun = "violation" if count == 1 else "violations"
        self._stdout.write(f"Found {count} {noun}.\n")


def main() -> None:
    """Console-script entry point."""
    raise SystemExit(LitmusCheckCommand().run(sys.argv[1:]))
