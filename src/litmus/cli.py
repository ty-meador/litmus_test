"""The ``litmus`` command line.

``litmus run <path-or-module>[::SuiteClass]`` loads one suite module, selects one
``LitmusTest`` subclass defined in it, runs it through the sealed lifecycle, and maps
the terminal state onto the exit status. One command the developer documents and the
Floor Technician types; an exit code the station script branches on without parsing
output.

Exit status is the station contract (``docs/litmus-run.md``):

- ``0`` — the root settled ``Pass``.
- ``1`` — the root settled ``Fail``.
- ``2`` — the root settled ``Aborted``.
- ``3`` — usage error: the invocation was wrong, or the suite could not be loaded,
  selected, or constructed. No run took place.

The exit contract and the ``--json`` record are public; this module's path and its
classes are not (``docs/api-stability.md``).
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import importlib.util
import json
import logging
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import ModuleType
from typing import NoReturn, TextIO, final

from litmus.base import LitmusTest
from litmus.errors import LitmusError
from litmus.junit import JunitXml
from litmus.lifecycle import State
from litmus.results import RunResult

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_ABORTED = 2
EXIT_USAGE = 3

_EXIT_BY_STATE: Mapping[State, int] = {
    State.PASS: EXIT_PASS,
    State.FAIL: EXIT_FAIL,
    State.ABORTED: EXIT_ABORTED,
}
_SELECTOR = "::"
_ARTIFACT_ROOT_VARIABLE = "LITMUS_ARTIFACT_ROOT"


class UsageError(Exception):
    """The invocation cannot reach a run. The message names what to change."""


class _Parser(argparse.ArgumentParser):
    """``argparse`` that reports a bad invocation through the exit contract.

    ``argparse`` exits with status 2 on its own; 2 means ``Aborted`` here, so every
    parse error is raised instead and rendered by the command as a usage error.
    """

    def error(self, message: str) -> NoReturn:
        raise UsageError(message)


@final
class SuiteLoader:
    """Resolve ``<path-or-module>[::SuiteClass]`` to one ``LitmusTest`` subclass.

    A source ending in ``.py``, or naming an existing file, is loaded standalone from
    disk; anything else is imported as a dotted module name. Without ``::SuiteClass``
    the module must define exactly one ``LitmusTest`` subclass of its own; with it, the
    named attribute may be any ``LitmusTest`` subclass bound in the module, imported or
    defined. Every failure raises ``UsageError`` with the fix in the message.
    """

    def load(self, target: str) -> type[LitmusTest]:
        """Return the suite class ``target`` selects, or raise ``UsageError``."""
        source, _, class_name = target.partition(_SELECTOR)
        if not source:
            raise UsageError(f"{target!r}: a suite file or module name must precede '::'")
        module = self._import(source)
        defined = self._defined_suites(module)
        if class_name:
            return self._named(module, defined, class_name)
        if len(defined) == 1:
            return next(iter(defined.values()))
        if not defined:
            raise UsageError(f"{source}: defines no LitmusTest subclass")
        names = ", ".join(defined)
        raise UsageError(
            f"{source}: defines {len(defined)} suites ({names}); "
            f"name one as {source}{_SELECTOR}SuiteClass"
        )

    def _import(self, source: str) -> ModuleType:
        path = Path(source)
        if source.endswith(".py") or path.exists():
            return self._import_file(source, path)
        try:
            return importlib.import_module(source)
        except ModuleNotFoundError as exc:
            raise UsageError(
                f"{source}: {exc}; give a path to a .py file or an importable module name"
            ) from exc
        except Exception as exc:  # the module's own import-time failure is the message
            raise UsageError(f"{source}: {type(exc).__name__}: {exc}") from exc

    def _import_file(self, source: str, path: Path) -> ModuleType:
        if not path.is_file():
            raise UsageError(f"{source}: no such file")
        resolved = path.resolve()
        name = self._free_module_name(resolved.stem)
        spec = importlib.util.spec_from_file_location(name, resolved)
        if spec is None or spec.loader is None:
            raise UsageError(f"{source}: not an importable Python file")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:  # the module's own import-time failure is the message
            del sys.modules[name]
            raise UsageError(f"{source}: {type(exc).__name__}: {exc}") from exc
        return module

    @staticmethod
    def _free_module_name(stem: str) -> str:
        # A suite file is registered under its stem so dataclasses and the failure
        # taxonomy resolve its module; a stem already taken (a test module of the same
        # name under pytest) must not be displaced.
        name = stem
        suffix = 1
        while name in sys.modules:
            name = f"{stem}_{suffix}"
            suffix += 1
        return name

    @staticmethod
    def _defined_suites(module: ModuleType) -> dict[str, type[LitmusTest]]:
        defined: dict[str, type[LitmusTest]] = {}
        for attribute, value in vars(module).items():
            if (
                not attribute.startswith("_")
                and isinstance(value, type)
                and issubclass(value, LitmusTest)
                and value is not LitmusTest
                and value.__module__ == module.__name__
            ):
                defined[attribute] = value
        return defined

    @staticmethod
    def _named(
        module: ModuleType, defined: Mapping[str, type[LitmusTest]], class_name: str
    ) -> type[LitmusTest]:
        value = getattr(module, class_name, None)
        if isinstance(value, type) and issubclass(value, LitmusTest) and value is not LitmusTest:
            return value
        names = ", ".join(defined) or "(none)"
        raise UsageError(
            f"{module.__name__}: no LitmusTest subclass named {class_name!r}; defined here: {names}"
        )


@final
class LitmusCommand:
    """Parse arguments, run one suite, render the verdict, return the exit status.

    The command holds no state between runs; construct it with the streams to write
    to and call :meth:`run` with the argument vector. Tests drive it exactly the way
    the console script does. The verdict line and the run log go to ``stderr``; only
    the ``--json`` record goes to ``stdout``, so a station can capture it alone.
    """

    def __init__(self, stdout: TextIO | None = None, stderr: TextIO | None = None) -> None:
        self._stdout = stdout if stdout is not None else sys.stdout
        self._stderr = stderr if stderr is not None else sys.stderr

    def run(self, argv: Sequence[str]) -> int:
        """Execute one invocation and return its exit status."""
        parser = self._parser()
        try:
            args = parser.parse_args(argv)
            suite = self._construct(SuiteLoader().load(args.target))
            self._attach_console_log()
            result = asyncio.run(suite.run(env=self._run_env(args.artifact_root)))
        except UsageError as exc:
            self._stderr.write(f"litmus run: error: {exc}\n")
            return EXIT_USAGE
        self._render(result, json_out=args.json, junit=args.junit)
        return _EXIT_BY_STATE[result.state]

    @staticmethod
    def _parser() -> argparse.ArgumentParser:
        parser = _Parser(
            prog="litmus",
            description=(
                "Run a Litmus suite and exit with its verdict: 0 Pass, 1 Fail, "
                "2 Aborted, 3 usage error."
            ),
        )
        commands = parser.add_subparsers(dest="command", required=True, metavar="command")
        run = commands.add_parser(
            "run",
            help="run one suite through the sealed lifecycle",
            description=(
                "Load TARGET, select one LitmusTest subclass, run it, and exit with "
                "its terminal state."
            ),
        )
        run.add_argument(
            "target",
            metavar="TARGET",
            help="suite file (.py) or importable module, optionally followed by ::SuiteClass",
        )
        run.add_argument(
            "--artifact-root",
            metavar="DIR",
            help=f"evidence root for this run; overrides {_ARTIFACT_ROOT_VARIABLE}",
        )
        run.add_argument(
            "--json",
            action="store_true",
            help="print the format_version 1 result record to stdout",
        )
        run.add_argument("--junit", metavar="PATH", help="also write the run as a JUnit XML report")
        return parser

    @staticmethod
    def _construct(suite_class: type[LitmusTest]) -> LitmusTest:
        try:
            return suite_class(name=suite_class.__name__)
        except TypeError as exc:
            raise UsageError(
                f"{suite_class.__qualname__} cannot be constructed from its name alone "
                f"({exc}); give every other __init__ parameter a default so a station "
                "can run it"
            ) from exc
        except LitmusError as exc:
            raise UsageError(f"{suite_class.__qualname__}: {exc}") from exc

    @staticmethod
    def _run_env(artifact_root: str | None) -> Mapping[str, str] | None:
        if artifact_root is None:
            return None
        return {**os.environ, _ARTIFACT_ROOT_VARIABLE: artifact_root}

    def _attach_console_log(self) -> None:
        # The per-node timeline is the record; the console is for the person at the
        # station. ``basicConfig`` is a no-op when a handler is already installed, so
        # a harness that owns logging (pytest included) keeps it.
        logging.basicConfig(level=logging.INFO, stream=self._stderr, format="%(message)s")
        logging.captureWarnings(True)

    def _render(self, result: RunResult, *, json_out: bool, junit: str | None) -> None:
        verdict = f"litmus run: {result.node}: {result.state.value}"
        if result.failure is not None:
            verdict = f"{verdict} ({type(result.failure).__name__}: {result.failure})"
        self._stderr.write(f"{verdict}\n")
        if junit is not None:
            JunitXml().write(result, Path(junit))
            self._stderr.write(f"litmus run: junit report written to {junit}\n")
        if json_out:
            # The same serialization as the framework's own ``result.json``.
            record = json.dumps(
                result.to_json(),
                allow_nan=False,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            self._stdout.write(f"{record}\n")


def main() -> None:
    """Console-script entry point."""
    raise SystemExit(LitmusCommand().run(sys.argv[1:]))
