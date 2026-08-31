"""Constructor-time hook registrations."""

import ast
import dis
import inspect
import math
import textwrap
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, cast

type HookFunction = Callable[[Any], Awaitable[object]]


def _require_coroutine(kind: str, fn: HookFunction) -> None:
    if not inspect.iscoroutinefunction(fn):
        msg = f"{kind} hook function must be defined with async def"
        raise TypeError(msg)


def _explicitly_returns_none(fn: HookFunction) -> bool:
    """Distinguish ``return None`` from an implicit fall-through.

    JavaScript's ``undefined`` made this distinction directly in the legacy hook
    contract. Python resolves both paths to ``None``, so registration records the
    authored return statement while the function is still available for inspection.
    Bytecode positions provide a fallback for dynamically defined functions whose
    source cannot be recovered.
    """
    function = getattr(fn, "__func__", fn)
    try:
        source = textwrap.dedent(inspect.getsource(function))
        tree = ast.parse(source)
    except OSError, TypeError, IndentationError, SyntaxError:
        instructions = tuple(dis.get_instructions(function))
        for index, instruction in enumerate(instructions):
            if (
                instruction.opname == "RETURN_CONST"
                and instruction.argval is None
                and instruction.starts_line
            ):
                return True
            if instruction.opname == "RETURN_VALUE" and index:
                previous = instructions[index - 1]
                if (
                    previous.opname == "LOAD_CONST"
                    and previous.argval is None
                    and previous.starts_line
                ):
                    return True
        return False

    target = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == function.__name__
        ),
        None,
    )
    if target is None:
        return False

    class _ReturnFinder(ast.NodeVisitor):
        found = False

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            if node is target:
                for statement in node.body:
                    self.visit(statement)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            return

        def visit_Lambda(self, node: ast.Lambda) -> None:
            return

        def visit_Return(self, node: ast.Return) -> None:
            if node.value is None or (
                isinstance(node.value, ast.Constant) and node.value.value is None
            ):
                self.found = True

    finder = _ReturnFinder()
    finder.visit(target)
    return finder.found


@dataclass(frozen=True, slots=True)
class SurveyHook:
    """A structurally valid survey registration."""

    fn: HookFunction
    captures_none: bool = field(init=False)

    def __post_init__(self) -> None:
        _require_coroutine("survey", self.fn)
        object.__setattr__(self, "captures_none", _explicitly_returns_none(self.fn))


@dataclass(frozen=True, slots=True)
class AuditHook:
    """A structurally valid interval audit registration."""

    interval: float
    fn: HookFunction
    captures_none: bool = field(init=False)

    def __post_init__(self) -> None:
        _require_coroutine("audit", self.fn)
        interval = cast("object", self.interval)
        if (
            isinstance(interval, bool)
            or not isinstance(interval, int | float)
            or not math.isfinite(interval)
            or interval <= 0
        ):
            raise ValueError("audit interval must be a positive finite number")
        object.__setattr__(self, "captures_none", _explicitly_returns_none(self.fn))
