"""The per-node utility surface handed to authored lifecycle code."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import uuid
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any, final

from litmus.artifacts import Artifact
from litmus.errors import ExternalIntegrationFailure, TestFailure
from litmus.executor import ExecResult, LocalExec
from litmus.hooks import IntegrationHooks
from litmus.lifecycle import AuditSignal, RunAbort
from litmus.remote import RemoteInterface
from litmus.routing import ExecRouter, ExecRouting, RouterRunParams, Shell

type WaitPredicate = Callable[[], bool | Awaitable[bool]]

LOGGER = logging.getLogger("litmus")


@final
class _RunTransports:
    """Every transport opened during one run, without changing node ownership."""

    __slots__ = ("_halted", "_late_closes", "_local_exec", "_remotes")

    def __init__(self, local_exec: LocalExec) -> None:
        self._local_exec = local_exec
        self._remotes: list[RemoteInterface] = []
        self._halted = False
        self._late_closes: set[asyncio.Task[None]] = set()

    def register(self, remote: RemoteInterface) -> None:
        """Remember an opened resource and hard-close late arrivals after an abort."""
        if all(known is not remote for known in self._remotes):
            self._remotes.append(remote)
        if self._halted and remote.connected:
            close = asyncio.create_task(self._close_remote(remote))
            self._late_closes.add(close)
            close.add_done_callback(self._late_closes.discard)

    async def cancel_in_flight(self) -> None:
        """Close local work and every known remote; one broken close cannot mask another."""
        self._halted = True
        await asyncio.gather(
            self._close_local(),
            *(self._close_remote(remote) for remote in tuple(self._remotes)),
        )
        while self._late_closes:
            await asyncio.gather(*tuple(self._late_closes))

    async def _close_local(self) -> None:
        try:
            await self._local_exec.cancel_in_flight()
        except Exception:
            LOGGER.exception("run-abort could not terminate a local subprocess")

    async def _close_remote(self, remote: RemoteInterface) -> None:
        try:
            await remote.cancel_in_flight()
        except Exception:
            LOGGER.exception("run-abort could not terminate remote %s", remote.endpoint)


@final
class _ArtifactStore:
    """Private persistence boundary shared by every node in one run."""

    __slots__ = ("_root",)

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).expanduser().resolve()

    @property
    def root(self) -> Path:
        """Absolute directory where this run's captured evidence is stored."""
        return self._root

    async def capture(self, artifact: Artifact) -> Path:
        """Persist one artifact without blocking the event loop or overwriting evidence."""
        return await asyncio.to_thread(self._capture_sync, artifact)

    def _capture_sync(self, artifact: Artifact) -> Path:
        """Serialize and atomically publish one artifact from an executor thread."""
        serialized = json.dumps(
            artifact.to_json(),
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        serialized = f"{serialized}\n"
        self._root.mkdir(parents=True, exist_ok=True)

        temporary = self._root / f".{uuid.uuid4().hex}.artifact.tmp"
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as artifact_file:
                artifact_file.write(serialized)
                artifact_file.flush()
                os.fsync(artifact_file.fileno())

            timestamp = artifact.timestamp.strftime("%Y%m%dT%H%M%S.%fZ")
            base_name = f"{artifact.name}.{artifact.type}.{timestamp}"
            collision = 0
            while True:
                suffix = "" if collision == 0 else f".{collision}"
                destination = self._root / f"{base_name}{suffix}.artifact"
                try:
                    os.link(temporary, destination)
                except FileExistsError:
                    collision += 1
                    continue
                return destination
        finally:
            temporary.unlink(missing_ok=True)


@final
class RunSignal:
    """Read-only view of the cancellation signals affecting one test node."""

    __slots__ = ("_audit_signal", "_run_abort")

    def __init__(self, run_abort: RunAbort, audit_signal: AuditSignal | None = None) -> None:
        """Expose observation without exposing either framework writer."""
        self._run_abort = run_abort
        self._audit_signal = audit_signal

    @property
    def fired(self) -> bool:
        """Whether integration or audit control flow has asked this node to stop."""
        return self.reason is not None

    @property
    def reason(self) -> ExternalIntegrationFailure | TestFailure | None:
        """The failure that asked this node to stop, if one exists."""
        if self._run_abort.reason is not None:
            return self._run_abort.reason
        return None if self._audit_signal is None else self._audit_signal.reason


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
        "_artifact_store",
        "_base_logger",
        "_ctx",
        "_env",
        "_integration_hooks",
        "_local_exec",
        "_logger",
        "_routing",
        "_scope",
        "_signal",
        "_transports",
    )

    def __init__(
        self,
        *,
        ctx: dict[str, Any] | None = None,
        env: Mapping[str, str] | None = None,
        logger: logging.Logger | None = None,
        artifact_root: str | Path | None = None,
        _integration_hooks: IntegrationHooks | None = None,
    ) -> None:
        """Create an unscoped run seed; lifecycle execution creates node views.

        Args:
            ctx: Initial root context. The framework mutates this dict in place.
            env: Environment snapshot. A defensive, immutable copy is retained.
            logger: Logging sink beneath the per-node scope adapter.
            artifact_root: Directory for point-in-time evidence. Defaults to the
                ``LITMUS_ARTIFACT_ROOT`` environment snapshot, then ``./artifacts``.
        """
        self._ctx = {} if ctx is None else ctx
        source_env = os.environ if env is None else env
        self._env = MappingProxyType(dict(source_env))
        configured_artifact_root = artifact_root
        if configured_artifact_root is None:
            configured_artifact_root = self._env.get(
                "LITMUS_ARTIFACT_ROOT", Path.cwd() / "artifacts"
            )
        self._artifact_store = _ArtifactStore(configured_artifact_root)
        self._base_logger = LOGGER if logger is None else logger
        self._scope: tuple[str, ...] = ()
        self._logger = ScopedLogger(self._base_logger, self._scope)
        self._local_exec = LocalExec()
        self._transports = _RunTransports(self._local_exec)
        self._integration_hooks = (
            IntegrationHooks() if _integration_hooks is None else _integration_hooks
        )
        self._routing = ExecRouting(
            self._local_exec.run,
            logger=self._logger,
            on_open=self._transports.register,
        )
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

    @property
    def artifact_root(self) -> Path:
        """Absolute directory where this run's captured evidence is stored."""
        return self._artifact_store.root

    async def capture_artifact(self, artifact: Artifact) -> Path:
        """Persist evidence and append its structured pointer to the scoped timeline.

        The file is durably published before the pointer is logged. Serialization or
        persistence failures therefore produce neither a misleading pointer nor a
        partially published artifact.
        """
        artifact_path = await self._artifact_store.capture(artifact)
        timestamp = artifact.timestamp.isoformat().replace("+00:00", "Z")
        self._logger.info(
            "artifact captured [%s:%s] -> %s",
            artifact.type,
            artifact.name,
            artifact_path,
            extra={
                "artifact_name": artifact.name,
                "artifact_path": str(artifact_path),
                "artifact_timestamp": timestamp,
                "artifact_type": artifact.type,
            },
        )
        return artifact_path

    async def exec(self, command: str) -> ExecResult:
        """Run ``command`` through this node's active route.

        Never raises for the command's own exit status. A non-zero `exit_code`
        is evidence for the test method to judge, not an exception. Signal-killed
        and aborted commands settle as `exit_code == -1`. `CancelledError` still
        propagates: that is TaskGroup teardown, not a command outcome.
        """
        self._logger.info("attempting to run command >%s<", command)
        result = await self._routing.exec(command)
        if result.stderr:
            self._logger.error("stderr: \n%s", result.stderr)
        self._logger.info("exit code: %s", result.exit_code)
        self._logger.info("stdout: \n%s", result.stdout)
        return result

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

    def _inherit_router(self, router: ExecRouter | None) -> None:
        """Install a via device's route so this node's exec borrows that hop."""
        if router is None:
            return
        self._routing.apply_run_params(RouterRunParams(router))

    async def _disconnect_opened(self) -> None:
        """Close remotes this node opened. Inherited hops stay with their owner."""
        await self._routing.disconnect_opened()

    def _halt_signal(self) -> RunAbort:
        """The run abort this node was scoped with. Framework lifecycle only."""
        return self._signal._run_abort  # pyright: ignore[reportPrivateUsage]

    def _for_node(
        self,
        node: str,
        run_abort: RunAbort,
        *,
        isolate_context: bool,
        audit_signal: AuditSignal | None = None,
        inherit_route: bool = True,
    ) -> TestUtils:
        """Create one node-owned view from this node's child inheritance policy."""
        scoped = object.__new__(TestUtils)
        scoped._artifact_store = self._artifact_store
        scoped._ctx = {} if isolate_context else self._ctx
        scoped._env = self._env
        scoped._base_logger = self._base_logger
        scoped._scope = (*self._scope, node)
        scoped._logger = ScopedLogger(self._base_logger, scoped._scope)
        scoped._local_exec = self._local_exec
        scoped._transports = self._transports
        scoped._integration_hooks = self._integration_hooks
        inherited = self._routing.child_run_params().router if inherit_route else None
        scoped._routing = ExecRouting(
            self._local_exec.run,
            inherited=inherited,
            logger=scoped._logger,
            on_open=self._transports.register,
        )
        scoped._signal = RunSignal(run_abort, audit_signal)
        return scoped

    async def _cancel_in_flight(self) -> None:
        """Hard-close every framework-managed transport known to this run."""
        await self._transports.cancel_in_flight()
