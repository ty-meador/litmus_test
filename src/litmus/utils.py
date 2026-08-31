"""The per-node utility surface handed to authored lifecycle code."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, final

from litmus.errors import ExternalIntegrationFailure, TestFailure
from litmus.executor import ExecResult, LocalExec
from litmus.lifecycle import RunAbort
from litmus.routing import ExecRouting, Shell

type WaitPredicate = Callable[[], bool | Awaitable[bool]]

LOGGER = logging.getLogger("litmus")


@final
class RunSignal:
    """Read-only view of the shared halt signal for one test run."""

    __slots__ = ("_run_abort",)

    def __init__(self, run_abort: RunAbort) -> None:
        """Expose observation without exposing the framework's ``fire`` method."""
        self._run_abort = run_abort

    @property
    def fired(self) -> bool:
        """Whether an external integration failure has halted the run."""
        return self._run_abort.fired

    @property
    def reason(self) -> ExternalIntegrationFailure | None:
        """The first integration failure that halted the run, if one exists."""
        return self._run_abort.reason


@final
class ScopedLogger(logging.LoggerAdapter[logging.Logger]):
    """A standard logger carrying the exact node path on every record."""

    __slots__ = ("_scope",)

    def __init__(self, logger: logging.Logger, scope: tuple[str, ...]) -> None:
        """Bind ``logger`` records to a root-to-leaf node path."""
        self._scope = scope
        node = scope[-1] if scope else ""
        super().__init__(
            logger,
            {"litmus_node": node, "litmus_scope": scope},
            merge_extra=True,
        )

    @property
    def scope(self) -> tuple[str, ...]:
        """Root-to-current-node path attached to every emitted record."""
        return self._scope

    @property
    def bread_crumbs(self) -> str:
        """Display form of :attr:`scope`, suitable for logs and artifact keys."""
        return "/".join(self._scope)


@final
class TestUtils:
    """The single fully scoped service object passed to every lifecycle method.

    A node owns the object, logger, and routing policy. Its authored composition
    receives distinct ``TestUtils`` objects which deliberately share that authored
    instance's mutable ``ctx``. A composed ``LitmusTest`` subclass instead begins with
    a fresh context, preserving instance locality.
    """

    __test__ = False
    __slots__ = (
        "_base_logger",
        "_ctx",
        "_env",
        "_local_exec",
        "_logger",
        "_routing",
        "_scope",
        "_signal",
    )

    def __init__(
        self,
        *,
        ctx: dict[str, Any] | None = None,
        env: Mapping[str, str] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        """Create an unscoped run seed; lifecycle execution creates node views.

        Args:
            ctx: Initial root context. The framework mutates this dict in place.
            env: Environment snapshot. A defensive, immutable copy is retained.
            logger: Logging sink beneath the per-node scope adapter.
        """
        self._ctx = {} if ctx is None else ctx
        source_env = os.environ if env is None else env
        self._env = MappingProxyType(dict(source_env))
        self._base_logger = LOGGER if logger is None else logger
        self._scope: tuple[str, ...] = ()
        self._logger = ScopedLogger(self._base_logger, self._scope)
        self._local_exec = LocalExec()
        self._routing = ExecRouting(self._local_exec.run, logger=self._logger)
        self._signal = RunSignal(RunAbort())

    @property
    def logger(self) -> ScopedLogger:
        """Logger whose records are tagged with this node's full scope."""
        return self._logger

    @property
    def ctx(self) -> dict[str, Any]:
        """This authored instance's mutable runtime context."""
        return self._ctx

    @property
    def env(self) -> Mapping[str, str]:
        """Immutable snapshot of the process environment for this run."""
        return self._env

    @property
    def signal(self) -> RunSignal:
        """Read-only view of the run-level integration halt signal."""
        return self._signal

    async def exec(self, command: str) -> ExecResult:
        """Run ``command`` through this node's active route."""
        return await self._routing.exec(command)

    async def wait_for(
        self,
        predicate: WaitPredicate,
        *,
        looking_for: str,
        interval: float = 1.0,
        timeout_seconds: float = 30.0,
    ) -> bool:
        """Poll until ``predicate`` succeeds or raise a structured timeout verdict."""
        if interval <= 0:
            raise ValueError("wait_for interval must be greater than zero")
        if timeout_seconds < 0:
            raise ValueError("wait_for timeout must not be negative")

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while True:
            observed = predicate()
            if inspect.isawaitable(observed):
                observed = await observed
            if observed:
                return True
            if loop.time() >= deadline:
                msg = f"timed out waiting for {looking_for}"
                raise TestFailure(msg, looking_for, "timeout")
            await asyncio.sleep(min(interval, max(0.0, deadline - loop.time())))

    async def use_shell(
        self,
        *,
        host: str | None = None,
        username: str | None = None,
        port: int = 22,
        password: str | None = None,
        private_key: str | bytes | None = None,
        private_key_path: str | Path | None = None,
        passphrase: str | None = None,
        agent: str | None = None,
        skip_host_verification: bool = False,
        via: Shell | None = None,
        inherit: bool = True,
        propagate: bool = True,
    ) -> Shell:
        """Declare this node's primary exec route and its child propagation policy."""
        return await self._routing.use_shell(
            host=host,
            username=username,
            port=port,
            password=password,
            private_key=private_key,
            private_key_path=private_key_path,
            passphrase=passphrase,
            agent=agent,
            skip_host_verification=skip_host_verification,
            via=via,
            inherit=inherit,
            propagate=propagate,
        )

    async def get_shell(
        self,
        *,
        host: str,
        username: str,
        port: int = 22,
        password: str | None = None,
        private_key: str | bytes | None = None,
        private_key_path: str | Path | None = None,
        passphrase: str | None = None,
        agent: str | None = None,
        skip_host_verification: bool = False,
        via: Shell | None = None,
        inherit: bool = True,
        propagate: bool = True,
    ) -> Shell:
        """Open an auxiliary endpoint without changing this node's exec route."""
        return await self._routing.get_shell(
            host=host,
            username=username,
            port=port,
            password=password,
            private_key=private_key,
            private_key_path=private_key_path,
            passphrase=passphrase,
            agent=agent,
            skip_host_verification=skip_host_verification,
            via=via,
            inherit=inherit,
            propagate=propagate,
        )

    def _log_exec_routing(self) -> None:
        """Record this node's topology line. The engine calls it once per executed node.

        Placed after `configure` for an authored node, since `use_shell` lives there
        and the route is not final until it returns.
        """
        self._logger.info("exec routing: %s", self._routing.routing_line())

    def _for_node(
        self,
        node: str,
        run_abort: RunAbort,
        *,
        isolate_context: bool,
    ) -> TestUtils:
        """Create one node-owned view from this node's child inheritance policy."""
        scoped = object.__new__(TestUtils)
        scoped._ctx = {} if isolate_context else self._ctx
        scoped._env = self._env
        scoped._base_logger = self._base_logger
        scoped._scope = (*self._scope, node)
        scoped._logger = ScopedLogger(self._base_logger, scoped._scope)
        scoped._local_exec = self._local_exec
        inherited = self._routing.child_run_params().router
        scoped._routing = ExecRouting(
            self._local_exec.run,
            inherited=inherited,
            logger=scoped._logger,
        )
        scoped._signal = RunSignal(run_abort)
        return scoped
