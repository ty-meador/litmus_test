"""The data model shared by every rule: a parsed suite module and the violations found.

Rules never touch the filesystem or the CLI. They receive a :class:`SuiteModule` — the
parsed AST plus the structural queries every rule needs — and yield
:class:`Violation` values. Keeping the queries here keeps each rule to its own idiom.
"""

from __future__ import annotations

import ast
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

type FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda

_BASE_CLASS_NAME = "LitmusTest"


@dataclass(frozen=True, slots=True, order=True)
class Violation:
    """One rule finding at one source location.

    ``line`` and ``column`` are 1-based, matching the ``path:line:col`` convention
    editors and CI log parsers already understand.
    """

    path: Path
    line: int
    column: int
    code: str
    message: str

    def render(self) -> str:
        """Format as ``path:line:col: code message``."""
        return f"{self.path}:{self.line}:{self.column}: {self.code} {self.message}"


class SuiteModule:
    """A parsed suite file with the structural queries the rules share.

    Contract:
      - The tree is parsed once; every node carries a parent link through
        :meth:`parent_of`, so rules walk upward without re-traversing.
      - :meth:`is_litmus_class` answers statically: a class is a Litmus test when it
        names ``LitmusTest`` (bare or attribute-qualified) as a base, or derives from
        another class in the same module that does. Cross-module inheritance is out
        of reach for a single-file analysis and is not guessed at.
    """

    def __init__(self, path: Path, source: str) -> None:
        self._path = path
        self._tree = ast.parse(source, filename=str(path))
        self._parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(self._tree):
            for child in ast.iter_child_nodes(parent):
                self._parents[child] = parent
        self._litmus_classes = self._resolve_litmus_classes()

    @property
    def path(self) -> Path:
        """Where the source was read from; reported on every violation."""
        return self._path

    @property
    def tree(self) -> ast.Module:
        """The parsed module."""
        return self._tree

    def parent_of(self, node: ast.AST) -> ast.AST | None:
        """Return the node's parent, or ``None`` for the module itself."""
        return self._parents.get(node)

    def ancestors(self, node: ast.AST) -> Iterator[ast.AST]:
        """Yield the node's ancestors nearest first, ending at the module."""
        current = self.parent_of(node)
        while current is not None:
            yield current
            current = self.parent_of(current)

    def enclosing_function(self, node: ast.AST) -> FunctionNode | None:
        """Return the nearest enclosing def or lambda, or ``None`` at module scope."""
        for ancestor in self.ancestors(node):
            if isinstance(ancestor, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
                return ancestor
        return None

    def enclosing_class(self, node: ast.AST) -> ast.ClassDef | None:
        """Return the nearest enclosing class, or ``None`` when there is none."""
        for ancestor in self.ancestors(node):
            if isinstance(ancestor, ast.ClassDef):
                return ancestor
        return None

    def enclosing_method(self, node: ast.AST) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
        """Return the nearest enclosing def that is a direct member of a class body."""
        for ancestor in self.ancestors(node):
            is_def = isinstance(ancestor, ast.FunctionDef | ast.AsyncFunctionDef)
            if is_def and isinstance(self.parent_of(ancestor), ast.ClassDef):
                return ancestor
        return None

    def is_litmus_class(self, node: ast.ClassDef) -> bool:
        """True when the class is statically known to derive from ``LitmusTest``."""
        return node in self._litmus_classes

    def litmus_classes(self) -> Iterator[ast.ClassDef]:
        """Yield every class in the module that derives from ``LitmusTest``."""
        for node in ast.walk(self._tree):
            if isinstance(node, ast.ClassDef) and node in self._litmus_classes:
                yield node

    def violation(self, node: ast.AST, code: str, message: str) -> Violation:
        """Build a violation anchored at ``node``."""
        line = getattr(node, "lineno", 1)
        column = getattr(node, "col_offset", 0) + 1
        return Violation(self._path, line, column, code, message)

    def _resolve_litmus_classes(self) -> frozenset[ast.ClassDef]:
        classes = [node for node in ast.walk(self._tree) if isinstance(node, ast.ClassDef)]
        known: set[str] = {_BASE_CLASS_NAME}
        resolved: set[ast.ClassDef] = set()
        changed = True
        while changed:
            changed = False
            for node in classes:
                if node in resolved:
                    continue
                if any(_base_name(base) in known for base in node.bases):
                    resolved.add(node)
                    known.add(node.name)
                    changed = True
        return frozenset(resolved)


def _base_name(base: ast.expr) -> str | None:
    if isinstance(base, ast.Name):
        return base.id
    if isinstance(base, ast.Attribute):
        return base.attr
    if isinstance(base, ast.Subscript):
        return _base_name(base.value)
    return None


class Rule(ABC):
    """The contract every ``litmus_check`` rule implements.

    A rule is stateless: :meth:`check` may be called on any number of modules in any
    order. ``code`` is the stable identifier the CLI selects and ignores by; it is the
    contract with suite authors' ``--ignore`` flags and must not be renamed casually.
    """

    code: str
    summary: str

    @abstractmethod
    def check(self, module: SuiteModule) -> Iterable[Violation]:
        """Yield every violation of this rule found in ``module``."""
