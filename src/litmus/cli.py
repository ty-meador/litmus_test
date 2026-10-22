"""The ``litmus`` command line.

``litmus run <path-or-module>[::SuiteClass]`` loads one suite module, selects one
``LitmusTest`` subclass defined in it, runs it through the sealed lifecycle, and maps
the terminal state onto the exit status. One command the developer documents and the
Floor Technician types; an exit code the station script branches on without parsing
output.

``litmus new SuiteName`` writes a gated-PASS suite and a matching self-test stub so
the author starts from a shape that already clears ``litmus-check`` and pyright
strict. The files are the single source for that shape (``docs/litmus-new.md``).

``litmus inspect [PATH]`` opens a saved run in the Inspector, the Floor Technician's
surface (``docs/inspector.md``). ``PATH`` is a run directory or its ``result.json``;
without it the most recently modified ``result.json`` under the artifact root opens,
the legacy discovery rule carried over by Ty's ruling of 2026-09-09 (issue #89). No
saved inspection runs suite code: what is not a saved version-1 run is refused with
the reader's diagnostic. The Re-Run tab is the deliberate exception: it executes the
suite ``litmus run`` recorded as ``suite_target``, or the one ``--suite`` names, and
only when the technician presses Enter on a node (Ty's ruling of 2026-09-10).

Exit status is the station contract (``docs/litmus-run.md``):

- ``0`` — the root settled ``Pass``, ``litmus new`` wrote the files, or the Inspector
  was closed.
- ``1`` — the root settled ``Fail``.
- ``2`` — the root settled ``Aborted``.
- ``3`` — usage error: the invocation was wrong, the suite could not be loaded,
  selected, or constructed, ``litmus new`` could not write, or ``litmus inspect``
  found nothing to open. No run took place.

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
from typing import Any, NoReturn, TextIO, cast, final

from litmus.base import LitmusTest
from litmus.errors import LitmusError
from litmus.inspector_app import InspectorApp
from litmus.inspector_artifacts import ArtifactDiagnostic
from litmus.inspector_rerun import RerunSession, SuiteFactory
from litmus.inspector_run import SavedRun, SavedRunReader
from litmus.junit import JunitXml
from litmus.lifecycle import State
from litmus.results import RunResult
from litmus.scaffold import ScaffoldError, SuiteScaffolder

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
_MODULE_SUITE = "__suite__"
_ARTIFACT_ROOT_VARIABLE = "LITMUS_ARTIFACT_ROOT"
_DEFAULT_ARTIFACT_ROOT = "artifacts"
_RESULT_FILE = "result.json"


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
    disk; anything else is imported as a dotted module name. With ``::SuiteClass`` the
    named attribute may be any ``LitmusTest`` subclass bound in the module, imported or
    defined. Without it, a module-level ``__suite__`` bound to such a class is the
    default; a module with neither must define exactly one ``LitmusTest`` subclass of
    its own. Every failure raises ``UsageError`` with the fix in the message.
    """

    def load(self, target: str) -> type[LitmusTest]:
        """Return the suite class ``target`` selects, or raise ``UsageError``."""
        source, _, class_name = target.partition(_SELECTOR)
        if not source:
            raise UsageError(
                f"{target!r}: a suite file or module name must precede '::'; "
                "give a path or module name before the selector"
            )
        module = self._import(source)
        defined = self._defined_suites(module)
        if class_name:
            return self._named(module, defined, class_name)
        if _MODULE_SUITE in vars(module):
            return self._declared(source, module, defined)
        if len(defined) == 1:
            return next(iter(defined.values()))
        if not defined:
            raise UsageError(
                f"{source}: defines no LitmusTest subclass; "
                f"define one in that module or name one as {source}{_SELECTOR}SuiteClass"
            )
        names = ", ".join(defined)
        raise UsageError(
            f"{source}: defines {len(defined)} suites ({names}); "
            f"name one as {source}{_SELECTOR}SuiteClass"
        )

    @staticmethod
    def construct(suite_class: type[LitmusTest]) -> LitmusTest:
        """Construct ``suite_class`` from its name alone, or raise ``UsageError``.

        The station has nothing but the name to give, so a suite the command line can
        run is one whose every other constructor parameter has a default.
        """
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
    def canonical(target: str, suite_class: type[LitmusTest]) -> str:
        """The target to record on the run: an absolute file or module name, and the class.

        The class is always named, even when the module defined only one, so the record
        still selects the same suite after a second one is added beside it. A file path
        is made absolute so ``litmus inspect`` from another directory finds it.
        """
        source, _, _ = target.partition(_SELECTOR)
        path = Path(source)
        if source.endswith(".py") or path.exists():
            source = str(path.resolve())
        return f"{source}{_SELECTOR}{suite_class.__name__}"

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
            raise UsageError(
                f"{source}: {type(exc).__name__}: {exc}; fix the import error in that module"
            ) from exc

    def _import_file(self, source: str, path: Path) -> ModuleType:
        if not path.is_file():
            raise UsageError(f"{source}: no such file; give a path to an existing .py file")
        resolved = path.resolve()
        name = self._free_module_name(resolved.stem)
        spec = importlib.util.spec_from_file_location(name, resolved)
        if spec is None or spec.loader is None:
            raise UsageError(f"{source}: not an importable Python file; give a readable .py file")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:  # the module's own import-time failure is the message
            del sys.modules[name]
            raise UsageError(
                f"{source}: {type(exc).__name__}: {exc}; fix the import error in that module"
            ) from exc
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
    def _defined_suites(module: ModuleType) -> dict[str, type[LitmusTest[Any]]]:
        # The CLI loads whatever suite the module defines, so it names the node type
        # without committing to any one suite's context shape (issue #75).
        defined: dict[str, type[LitmusTest[Any]]] = {}
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
    def _as_suite(value: object) -> type[LitmusTest[Any]] | None:
        if isinstance(value, type) and issubclass(value, LitmusTest) and value is not LitmusTest:
            # The checks above are what prove the type. Narrowing a generic class
            # leaves its parameter unsolved, so the CLI states what it already knows:
            # a suite class of any context shape (issue #75).
            return cast("type[LitmusTest[Any]]", value)
        return None

    def _named(
        self, module: ModuleType, defined: Mapping[str, type[LitmusTest[Any]]], class_name: str
    ) -> type[LitmusTest[Any]]:
        suite = self._as_suite(getattr(module, class_name, None))
        if suite is not None:
            return suite
        names = ", ".join(defined) or "(none)"
        raise UsageError(
            f"{module.__name__}: no LitmusTest subclass named {class_name!r}; name one of: {names}"
        )

    def _declared(
        self, source: str, module: ModuleType, defined: Mapping[str, type[LitmusTest[Any]]]
    ) -> type[LitmusTest[Any]]:
        # ``__suite__`` is the module's own ``::SuiteClass``: the file names its default
        # when it holds more than one suite, and the selector still overrides (#160).
        value = getattr(module, _MODULE_SUITE)
        suite = self._as_suite(value)
        if suite is not None:
            return suite
        names = ", ".join(defined) or "(none)"
        raise UsageError(
            f"{source}: {_MODULE_SUITE} is {value!r}, not a LitmusTest subclass; "
            f"bind it to one of: {names}"
        )


@final
class SavedRunLocator:
    """Resolve ``litmus inspect [PATH]`` to the one ``result.json`` it opens.

    ``PATH`` is a run directory or its ``result.json``; a directory selects the
    ``result.json`` inside it. Without ``PATH`` the most recently modified
    ``result.json`` under the artifact root is chosen, the legacy discovery rule (Ty's
    ruling of 2026-09-09 on issue #89). Nothing is validated here: the saved-run reader
    owns that boundary, so an absent or damaged selection becomes its diagnostic rather
    than a guess at some other run. With nothing under the root there is no saved tree
    to inspect, and the answer says so instead of starting a run to manufacture one.
    """

    def locate(self, path: str | None, *, artifact_root: Path) -> Path:
        """Return the ``result.json`` to open, or raise ``UsageError`` naming the root."""
        if path is not None:
            selected = Path(path)
            return selected / _RESULT_FILE if selected.is_dir() else selected
        candidates = [
            candidate for candidate in artifact_root.rglob(_RESULT_FILE) if candidate.is_file()
        ]
        if not candidates:
            raise UsageError(
                f"no saved run under {artifact_root}: nothing to inspect; run a suite first, "
                f"or give a run directory or its {_RESULT_FILE}"
            )
        return max(candidates, key=lambda candidate: candidate.stat().st_mtime)


@final
class LitmusCommand:
    """Parse arguments, dispatch ``run``, ``new``, or ``inspect``, return the exit status.

    The command holds no state between runs; construct it with the streams to write
    to and call :meth:`run` with the argument vector. Tests drive it exactly the way
    the console script does. For ``run``, the verdict line and the run log go to
    ``stderr``; only the ``--json`` record goes to ``stdout``, so a station can
    capture it alone. For ``new``, the written paths go to ``stdout``. For ``inspect``,
    the terminal belongs to the Inspector until it is closed; a refusal goes to
    ``stderr`` and nothing is opened.
    """

    def __init__(self, stdout: TextIO | None = None, stderr: TextIO | None = None) -> None:
        self._stdout = stdout if stdout is not None else sys.stdout
        self._stderr = stderr if stderr is not None else sys.stderr

    def run(self, argv: Sequence[str]) -> int:
        """Execute one invocation and return its exit status."""
        parser = self._parser()
        prefix = "litmus"
        try:
            args = parser.parse_args(argv)
            prefix = f"litmus {args.command}"
            if args.command == "new":
                return self._new(args)
            if args.command == "inspect":
                return self._inspect(args)
            return self._run_suite(args)
        except UsageError as exc:
            self._stderr.write(f"{prefix}: error: {exc}\n")
            return EXIT_USAGE

    def _inspect(self, args: argparse.Namespace) -> int:
        """Open the selected or latest saved run in the Inspector.

        Opening runs no suite code. The Re-Run tab gets a factory for the suite, not
        a suite: an explicit ``--suite`` is loaded now so a wrong target is a usage
        error at the prompt, while the recorded ``suite_target`` is loaded only when
        the technician first opens that tab, so inspection alone still imports nothing.
        """
        root = args.artifact_root
        if root is None:
            root = os.environ.get(_ARTIFACT_ROOT_VARIABLE, _DEFAULT_ARTIFACT_ROOT)
        selected = SavedRunLocator().locate(args.path, artifact_root=Path(root))
        saved = asyncio.run(SavedRunReader().read(selected))
        if isinstance(saved, ArtifactDiagnostic):
            # The reader's words are the refusal: what was selected, and why it is not
            # a saved version-1 run. No other file is opened in its place.
            raise UsageError(f"{saved.reference}: {saved.message}")
        session = RerunSession(saved, self._suite_factory(saved, args.suite))
        try:
            InspectorApp(saved, session).run()
        finally:
            # Re-run evidence is scratch by ruling: nothing the technician produced
            # from that tab outlives the Inspector, however it closed.
            session.close()
        return EXIT_PASS

    @staticmethod
    def _suite_factory(saved: SavedRun, explicit: str | None) -> SuiteFactory | str:
        loader = SuiteLoader()
        if explicit is not None:
            suite_class = loader.load(explicit)
            return lambda: loader.construct(suite_class)
        recorded = saved.tree.result.get("suite_target")
        if not isinstance(recorded, str):
            return (
                "this run did not record the suite that produced it; "
                "reopen with --suite PATH[::SuiteClass] to re-run its nodes"
            )
        return lambda: loader.construct(loader.load(recorded))

    def _run_suite(self, args: argparse.Namespace) -> int:
        """Load, construct, and run the suite ``args.target`` selects."""
        loader = SuiteLoader()
        suite_class = loader.load(args.target)
        suite = loader.construct(suite_class)
        self._attach_console_log()
        result = asyncio.run(
            suite.run(
                env=self._run_env(args.artifact_root),
                suite_target=loader.canonical(args.target, suite_class),
            )
        )
        self._render(result, json_out=args.json, junit=args.junit)
        return _EXIT_BY_STATE[result.state]

    def _new(self, args: argparse.Namespace) -> int:
        """Write a gated-PASS suite and its self-test into ``args.directory``."""
        scaffolder = SuiteScaffolder()
        try:
            plan = scaffolder.plan(args.name, directory=Path(args.directory))
            scaffolder.write(plan, force=args.force)
        except ScaffoldError as exc:
            raise UsageError(str(exc)) from exc
        self._stdout.write(f"wrote {plan.suite_path}\n")
        self._stdout.write(f"wrote {plan.test_path}\n")
        return 0

    @staticmethod
    def _parser() -> argparse.ArgumentParser:
        parser = _Parser(
            prog="litmus",
            description=(
                "Run a Litmus suite, write a gated-PASS suite, or inspect a saved run. "
                "Exit 0 Pass (or files written, or the Inspector closed), 1 Fail, "
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
        new = commands.add_parser(
            "new",
            help="write a gated-PASS suite and a matching self-test stub",
            description=(
                "Write SUITE.py and test_SUITE.py: annotated constructor, configure "
                "returning a narrowed ctx, a pure build_suite, one gated test method, "
                "and cleanup. The output passes litmus-check and pyright strict."
            ),
        )
        new.add_argument(
            "name",
            metavar="SUITE",
            help="class name of the suite to write, for example WidgetSuite",
        )
        new.add_argument(
            "--directory",
            metavar="DIR",
            default=".",
            help="directory to write into (default: the current directory)",
        )
        new.add_argument(
            "--force",
            action="store_true",
            help="overwrite existing files instead of refusing",
        )
        inspect_parser = commands.add_parser(
            "inspect",
            help="open a saved run in the Inspector",
            description=(
                "Open PATH, a run directory or its result.json, in the Inspector. Without "
                "PATH, open the most recently modified result.json under the artifact "
                "root. Opening runs no suite code; anything that is not a saved version-1 "
                "run is refused with the reader's diagnostic. The Re-Run tab executes "
                "the suite the run recorded, or the one --suite names, only on Enter."
            ),
        )
        inspect_parser.add_argument(
            "path",
            metavar="PATH",
            nargs="?",
            help="run directory or its result.json (default: the latest run under the root)",
        )
        inspect_parser.add_argument(
            "--artifact-root",
            metavar="DIR",
            help=(
                f"where the latest run is looked for; overrides {_ARTIFACT_ROOT_VARIABLE} "
                f"(default: {_DEFAULT_ARTIFACT_ROOT})"
            ),
        )
        inspect_parser.add_argument(
            "--suite",
            metavar="TARGET",
            help=(
                "suite the Re-Run tab executes, as for litmus run "
                "(default: the suite_target the run recorded)"
            ),
        )
        return parser

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
            # The run has settled and ``result.json`` is written; a report path that
            # cannot be written is an error line, not a traceback that exits 1 and
            # reads as Fail. The exit status stays the verdict's.
            try:
                JunitXml().write(result, Path(junit))
            except OSError as exc:
                self._stderr.write(
                    f"litmus run: error: junit report not written to {junit}: {exc}\n"
                )
            else:
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
