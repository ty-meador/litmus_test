"""Hook registrations and deliberately narrow integration contexts.

Execution belongs to the Phase 2 hook engine. These immutable records establish the
shape now, so an incomplete registration cannot survive construction and reach hardware.
"""

import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from litmus.results import RunResult

type HookFunction = Callable[[Any], Awaitable[object]]
type BeforeEachHook = Callable[["BeforeEachContext"], Awaitable[None]]
type AfterEachHook = Callable[["AfterEachContext"], Awaitable[None]]


def _require_coroutine(kind: str, fn: HookFunction) -> None:
    if not inspect.iscoroutinefunction(fn):
        msg = f"{kind} hook function must be defined with async def"
        raise TypeError(msg)


@dataclass(frozen=True, slots=True)
class SurveyHook:
    """A structurally valid survey registration."""

    fn: HookFunction

    def __post_init__(self) -> None:
        _require_coroutine("survey", self.fn)


@dataclass(frozen=True, slots=True)
class AuditHook:
    """A structurally valid interval audit registration."""

    interval: float
    fn: HookFunction

    def __post_init__(self) -> None:
        _require_coroutine("audit", self.fn)


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
