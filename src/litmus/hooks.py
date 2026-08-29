"""Constructor-time hook registrations.

Execution belongs to the Phase 2 hook engine. These immutable records establish the
shape now, so an incomplete registration cannot survive construction and reach hardware.
"""

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

type HookFunction = Callable[[Any], Awaitable[object]]


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
