"""Hook registrations and deliberately narrow integration contexts.

The lifecycle engine dispatches these hooks around every executed node. These immutable
records keep incomplete registrations from surviving construction and reaching hardware.
"""

import ast
import dis
import inspect
import logging
import math
import textwrap
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, cast

from litmus.results import RunResult

type HookFunction = Callable[[Any], Awaitable[object]]
type BeforeEachHook = Callable[["BeforeEachContext"], Awaitable[None]]
type AfterEachHook = Callable[["AfterEachContext"], Awaitable[None]]


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


@dataclass(frozen=True, slots=True)
class BeforeEachContext:
    """External-coordination context that cannot exercise the target hardware."""

    name: str
    ancestor_names: tuple[str, ...]
    bread_crumbs: str
    logger: logging.LoggerAdapter[logging.Logger]
    env: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class AfterEachContext(BeforeEachContext):
    """The narrow integration context plus this node's completed result."""

    result: RunResult


@dataclass(frozen=True, slots=True)
class IntegrationHooks:
    """One root registration shared by reference across an entire run."""

    before_each: BeforeEachHook | None = None
    after_each: AfterEachHook | None = None
    allow_unpaired: bool = False

    def __post_init__(self) -> None:
        for kind, hook in (
            ("before_each", self.before_each),
            ("after_each", self.after_each),
        ):
            if hook is not None and not callable(hook):
                msg = f"{kind} hook must be callable"
                raise TypeError(msg)

    def warn_if_unpaired(self, logger: logging.LoggerAdapter[logging.Logger]) -> None:
        """Teach the pair invariant once, at the root where registration occurs."""
        has_before = self.before_each is not None
        has_after = self.after_each is not None
        if has_before is has_after or self.allow_unpaired:
            return
        present = "before_each" if has_before else "after_each"
        missing = "after_each" if has_before else "before_each"
        logger.warning(
            "UNPAIRED INTEGRATION HOOK — registered %s without %s. "
            "before_each and after_each are designed as a pair: before_each writes "
            "started records and after_each writes finished records. Running only "
            "one half can leave orphan records in the external integration system. "
            "If this asymmetry is intentional, pass allow_unpaired_hooks=True.",
            present,
            missing,
        )


async def invoke_integration_hook(
    kind: str,
    hook: BeforeEachHook | AfterEachHook,
    context: BeforeEachContext | AfterEachContext,
) -> None:
    """Await one hook while keeping calls and invalid returns inside the boundary."""
    returned: object = hook(context)  # pyright: ignore[reportArgumentType]
    if not inspect.isawaitable(returned):
        msg = f"{kind} hook must return an awaitable"
        raise TypeError(msg)
    await returned
