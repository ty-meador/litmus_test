"""Refuse a runtime import cycle inside the package; report an annotation-only one. Not shipped.

STANDARDS.md section 1 says no import cycles, and section 3 draws the line this script
enforces: a cycle that exists only under ``if TYPE_CHECKING:`` is permitted, because
those imports never execute and exist so annotations can name a sibling's class; a
cycle among the imports that run is not, because the module that wins the race gets a
half-initialised sibling and the failure surfaces as an ``ImportError`` far from its
cause. ``make check`` runs this after ``pyright`` so a new edge is refused in the same
gate that types it.

The graph is built from the source with ``ast``, not by importing the package, so the
check cannot be fooled by import order and runs on a tree that does not import cleanly.
Only edges between modules of the package under test count; the standard library and
third parties cannot close a cycle through us.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import TextIO

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "litmus"

EXIT_CLEAN = 0
EXIT_CYCLE = 1

_TYPE_CHECKING = "TYPE_CHECKING"
_INIT = "__init__"
_BLOCK_STATEMENTS = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.With,
    ast.AsyncWith,
    ast.For,
    ast.AsyncFor,
    ast.While,
)


class ImportGraph:
    """The package-internal import edges of one package, split by when they run.

    ``runtime`` holds the edges that execute at import time; ``annotation`` holds the
    edges declared under ``if TYPE_CHECKING:``. :meth:`report` prints one line per
    cycle and returns the process exit code: 1 when any runtime cycle exists, 0
    otherwise, with annotation-only cycles listed so the reader knows they are there
    and knows they are permitted.
    """

    def __init__(
        self,
        *,
        runtime: Mapping[str, frozenset[str]],
        annotation: Mapping[str, frozenset[str]],
    ) -> None:
        self._runtime = {name: frozenset(edges) for name, edges in runtime.items()}
        self._annotation = {name: frozenset(edges) for name, edges in annotation.items()}

    @classmethod
    def from_package(cls, package: Path = PACKAGE) -> ImportGraph:
        """Parse every module directly under ``package`` and collect its internal edges."""
        modules = {path.stem for path in package.glob("*.py")}
        runtime: dict[str, set[str]] = {name: set() for name in modules}
        annotation: dict[str, set[str]] = {name: set() for name in modules}
        for path in sorted(package.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for target, under_type_checking in _ImportWalker(package.name, modules).edges(tree):
                bucket = annotation if under_type_checking else runtime
                bucket[path.stem].add(target)
        return cls(
            runtime={name: frozenset(edges) for name, edges in runtime.items()},
            annotation={name: frozenset(edges) for name, edges in annotation.items()},
        )

    @property
    def modules(self) -> tuple[str, ...]:
        """Every module in the graph, sorted."""
        return tuple(sorted(self._runtime))

    def runtime_cycles(self) -> tuple[tuple[str, ...], ...]:
        """Strongly connected components of the runtime edges with more than one way in."""
        return _cycles(self._runtime)

    def annotation_cycles(self) -> tuple[tuple[str, ...], ...]:
        """Cycles that close only once the ``TYPE_CHECKING`` edges are added."""
        merged = {
            name: self._runtime[name] | self._annotation.get(name, frozenset())
            for name in self._runtime
        }
        runtime = set(self.runtime_cycles())
        return tuple(cycle for cycle in _cycles(merged) if cycle not in runtime)

    def report(self, out: TextIO = sys.stdout) -> int:
        """Print the verdict on the graph; red only on a runtime cycle."""
        runtime = self.runtime_cycles()
        annotation = self.annotation_cycles()
        print(
            f"import cycles: {len(self.modules)} modules, "
            f"{len(runtime)} runtime, {len(annotation)} annotation-only",
            file=out,
        )
        for cycle in annotation:
            print(f"  annotation-only, permitted (STANDARDS §3): {', '.join(cycle)}", file=out)
        for cycle in runtime:
            print(f"  RUNTIME CYCLE among: {', '.join(cycle)}", file=out)
        if runtime:
            print(
                "runtime import cycle: break it or move the import under TYPE_CHECKING "
                "(STANDARDS.md section 3)",
                file=out,
            )
            return EXIT_CYCLE
        return EXIT_CLEAN


class _ImportWalker:
    """Yield ``(target module, under TYPE_CHECKING)`` for every internal import in a tree.

    Imports are read wherever they sit; the package has no function-local imports and a
    lazy one would still be an edge the next reader has to know about. The
    ``TYPE_CHECKING`` guard is recognised bare or ``typing``-qualified.
    """

    def __init__(self, package: str, modules: frozenset[str] | set[str]) -> None:
        self._package = package
        self._modules = frozenset(modules)

    def edges(self, tree: ast.Module) -> Iterator[tuple[str, bool]]:
        yield from self._walk(tree.body, under_type_checking=False)

    def _walk(
        self, statements: list[ast.stmt], *, under_type_checking: bool
    ) -> Iterator[tuple[str, bool]]:
        for statement in statements:
            if isinstance(statement, ast.If):
                guarded = under_type_checking or _is_type_checking(statement.test)
                yield from self._walk(statement.body, under_type_checking=guarded)
                yield from self._walk(statement.orelse, under_type_checking=under_type_checking)
            elif isinstance(statement, ast.Try):
                blocks = [statement.body, statement.orelse, statement.finalbody]
                blocks.extend(handler.body for handler in statement.handlers)
                for block in blocks:
                    yield from self._walk(block, under_type_checking=under_type_checking)
            elif isinstance(statement, _BLOCK_STATEMENTS):
                yield from self._walk(statement.body, under_type_checking=under_type_checking)
                orelse = getattr(statement, "orelse", [])
                yield from self._walk(orelse, under_type_checking=under_type_checking)
            else:
                for target in self._targets(statement):
                    yield target, under_type_checking

    def _targets(self, statement: ast.stmt) -> Iterator[str]:
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                yield from self._absolute(alias.name)
        elif isinstance(statement, ast.ImportFrom):
            if statement.level:
                yield from self._relative(statement)
            elif statement.module is not None:
                yield from self._absolute(statement.module, statement.names)

    def _absolute(self, dotted: str, names: list[ast.alias] | None = None) -> Iterator[str]:
        head, _, rest = dotted.partition(".")
        if head != self._package:
            return
        if rest:
            module = rest.split(".", 1)[0]
            if module in self._modules:
                yield module
            return
        if names is None:
            # ``import litmus`` runs the package root, which is a module like any other.
            yield _INIT
            return
        for alias in names:
            # ``from litmus import x`` reaches a submodule, or a name the root re-exports.
            yield alias.name if alias.name in self._modules else _INIT

    def _relative(self, statement: ast.ImportFrom) -> Iterator[str]:
        if statement.level != 1:
            return
        if statement.module is not None:
            module = statement.module.split(".", 1)[0]
            if module in self._modules:
                yield module
            return
        for alias in statement.names:
            if alias.name in self._modules:
                yield alias.name


def _is_type_checking(test: ast.expr) -> bool:
    if isinstance(test, ast.Name):
        return test.id == _TYPE_CHECKING
    return isinstance(test, ast.Attribute) and test.attr == _TYPE_CHECKING


def _cycles(edges: Mapping[str, frozenset[str]]) -> tuple[tuple[str, ...], ...]:
    """Tarjan's strongly connected components, keeping only the ones that are cycles.

    A component is a cycle when it has more than one module or a module imports itself.
    Members are sorted so the output is stable across runs.
    """
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    found: list[tuple[str, ...]] = []
    counter = 0

    def visit(node: str) -> None:
        nonlocal counter
        index[node] = low[node] = counter
        counter += 1
        stack.append(node)
        on_stack.add(node)
        for target in sorted(edges.get(node, frozenset())):
            if target not in index:
                visit(target)
                low[node] = min(low[node], low[target])
            elif target in on_stack:
                low[node] = min(low[node], index[target])
        if low[node] == index[node]:
            component: list[str] = []
            while True:
                member = stack.pop()
                on_stack.discard(member)
                component.append(member)
                if member == node:
                    break
            is_cycle = len(component) > 1 or node in edges.get(node, frozenset())
            if is_cycle:
                found.append(tuple(sorted(component)))

    for node in sorted(edges):
        if node not in index:
            visit(node)
    return tuple(sorted(found))


def main(argv: list[str] | None = None) -> int:
    """Entry point: optional package directory, default ``src/litmus``."""
    args = sys.argv[1:] if argv is None else argv
    package = Path(args[0]) if args else PACKAGE
    return ImportGraph.from_package(package).report()


if __name__ == "__main__":
    sys.exit(main())
