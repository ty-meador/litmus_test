"""The per-node utility surface handed to authored lifecycle code."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast, final

from litmus.artifacts import Artifact
from litmus.errors import ExternalIntegrationFailure, FrameworkContractViolation, TestFailure
from litmus.executor import ExecResult, LocalExec
from litmus.hooks import IntegrationHooks
from litmus.lifecycle import AuditSignal, RunAbort
from litmus.remote import RemoteInterface
from litmus.routing import ExecRouter, ExecRouting, RouterRunParams, Shell
from litmus.timeline import RunTimeline, TimelinePaths

type WaitPredicate = Callable[[], bool | Awaitable[bool]]

LOGGER = logging.getLogger("litmus")


@final
class _EnvFilter:
    """Decides which variables of the run environment are published as evidence.

    At most one of ``allow`` or ``deny`` may be given; both is a ``ValueError``.
    Patterns are regular expressions matched with ``re.search`` against the variable
    *name* — ``TOKEN`` matches ``GITLAB_TOKEN``; anchor as ``^HOME$`` for an exact name.
    When the source environment carries ``environment=DEBUG`` the lists are ignored and
    every variable is published, and a warning says so — an artifact that carries more
    than the operator configured must state why.
    """

    DEBUG_KEY = "environment"
    DEBUG_VALUE = "DEBUG"

    def __init__(self, *, allow: Iterable[str] | None, deny: Iterable[str] | None) -> None:
        if allow is not None and deny is not None:
            msg = (
                "env_allow and env_deny are mutually exclusive: "
                "publish by allow list or by deny list, never both"
            )
            raise ValueError(msg)
        self._mode: str | None = None
        patterns: Iterable[str] = ()
        if allow is not None:
            self._mode = "allow"
            patterns = allow
        elif deny is not None:
            self._mode = "deny"
            patterns = deny
        self._patterns: tuple[re.Pattern[str], ...] = tuple(re.compile(p) for p in patterns)

    @property
    def mode(self) -> str | None:
        """``"allow"``, ``"deny"``, or ``None`` when the snapshot is published verbatim."""
        return self._mode

    def apply(self, source: Mapping[str, str], logger: logging.Logger) -> dict[str, str]:
        """Return the variables to publish, logging when DEBUG mode overrides the list."""
        snapshot = dict(source)
        if self._mode is None:
            return snapshot
        if snapshot.get(self.DEBUG_KEY) == self.DEBUG_VALUE:
            logger.warning(
                "DEBUG mode detected, ignoring provided %s list and including ALL "
                "environment variables",
                self._mode,
            )
            return snapshot
        if self._mode == "allow":
            return {name: value for name, value in snapshot.items() if self._matches(name)}
        return {name: value for name, value in snapshot.items() if not self._matches(name)}

    def _matches(self, name: str) -> bool:
        return any(pattern.search(name) for pattern in self._patterns)


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

    __slots__ = ("_base_root", "_root")

    def __init__(self, root: str | Path) -> None:
        self._base_root = Path(root).expanduser().resolve()
        self._root = self._base_root

    @property
    def root(self) -> Path:
        """Absolute directory where this run's captured evidence is stored."""
        return self._root

    def begin_run(self) -> Path:
        """Create and select one collision-safe timestamped run directory."""
        self._base_root.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        collision = 0
        while True:
            suffix = "" if collision == 0 else f".{collision}"
            run_root = self._base_root / f"{timestamp}{suffix}"
            try:
                run_root.mkdir()
            except FileExistsError:
                collision += 1
                continue
            self._root = run_root
            return run_root

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

    __slots__ = ("_scope", "_timeline", "_timeline_paths")

    def __init__(
        self,
        logger: logging.Logger,
        scope: tuple[str, ...],
        *,
        timeline: RunTimeline | None = None,
        timeline_paths: TimelinePaths | None = None,
    ) -> None:
        """Bind ``logger`` records to a root-to-leaf node path."""
        self._scope = scope
        self._timeline = timeline
        self._timeline_paths = timeline_paths
        node = scope[-1] if scope else ""
        super().__init__(
            logger,
            {"litmus_node": node, "litmus_scope": scope},
            merge_extra=True,
        )

    def isEnabledFor(self, level: int) -> bool:  # noqa: N802
        """Keep every level live when the durable timeline is attached."""
        if self._timeline is not None and self._timeline_paths is not None:
            return True
        return super().isEnabledFor(level)

    def log(self, level: int, msg: object, *args: object, **kwargs: Any) -> None:
        """Persist the record before offering it to the process logging policy."""
        if self._timeline is not None and self._timeline_paths is not None:
            formatting_warning: str | None = None
            try:
                raw_message = str(msg)
                if len(args) == 1 and isinstance(args[0], Mapping) and args[0]:
                    message = raw_message % args[0]
                else:
                    message = raw_message % args if args else raw_message
            except Exception as error:
                try:
                    raw_message = str(msg)
                except Exception:
                    raw_message = f"<unprintable {type(msg).__name__}>"
                try:
                    rendered_args = repr(args)
                except Exception:
                    rendered_args = "<unprintable arguments>"
                message = f"{raw_message} {rendered_args}"
                formatting_warning = (
                    "logging message formatting failed "
                    f"({type(error).__name__}); preserved the unformatted "
                    "message and arguments"
                )
            supplied_extra = kwargs.get("extra")
            extra: dict[str, Any] | None = None
            if isinstance(supplied_extra, Mapping):
                extra = dict(cast("Mapping[str, Any]", supplied_extra))
            self._timeline.write(
                self._timeline_paths,
                scope=self._scope,
                level=level,
                message=message,
                extra=extra,
            )
            if formatting_warning is not None:
                self._timeline.write(
                    self._timeline_paths,
                    scope=self._scope,
                    level=logging.WARNING,
                    message=formatting_warning,
                    extra=None,
                )
        super().log(level, msg, *args, **kwargs)

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
        "_env_filter_warning",
        "_integration_hooks",
        "_local_exec",
        "_logger",
        "_owns_timeline",
        "_routing",
        "_scope",
        "_signal",
        "_timeline",
        "_timeline_paths",
        "_transports",
    )

    def __init__(
        self,
        *,
        ctx: dict[str, Any] | None = None,
        env: Mapping[str, str] | None = None,
        env_allow: Iterable[str] | None = None,
        env_deny: Iterable[str] | None = None,
        logger: logging.Logger | None = None,
        artifact_root: str | Path | None = None,
        _integration_hooks: IntegrationHooks | None = None,
    ) -> None:
        """Create an unscoped run seed; lifecycle execution creates node views.

        Raises ``ValueError`` when both ``env_allow`` and ``env_deny`` are given.

        Args:
            ctx: Initial root context. The framework mutates this dict in place.
            env: Environment source. Defaults to the process environment. The
                published snapshot is an immutable copy, filtered per the lists below.
            env_allow: Regular expressions; only variables whose *name* matches one
                (``re.search``) are published. Exclusive with ``env_deny``.
            env_deny: Regular expressions; variables whose name matches one are
                withheld. Exclusive with ``env_allow``.
                With either list, ``environment=DEBUG`` in the source publishes every
                variable and logs a warning saying so.
            logger: Logging sink beneath the per-node scope adapter.
            artifact_root: Directory for point-in-time evidence. Defaults to the
                ``LITMUS_ARTIFACT_ROOT`` environment snapshot, then ``./artifacts``.
        """
        self._ctx = {} if ctx is None else ctx
        self._base_logger = LOGGER if logger is None else logger
        self._scope: tuple[str, ...] = ()
        self._logger = ScopedLogger(self._base_logger, self._scope)
        self._timeline: RunTimeline | None = None
        self._timeline_paths: TimelinePaths | None = None
        self._owns_timeline = False
        source_env = os.environ if env is None else env
        env_filter = _EnvFilter(allow=env_allow, deny=env_deny)
        self._env = MappingProxyType(env_filter.apply(source_env, self._base_logger))
        self._env_filter_warning: str | None = None
        if (
            env_filter.mode is not None
            and source_env.get(_EnvFilter.DEBUG_KEY) == _EnvFilter.DEBUG_VALUE
        ):
            self._env_filter_warning = (
                f"DEBUG mode detected, ignoring provided {env_filter.mode} list "
                "and including ALL environment variables"
            )
        configured_artifact_root = artifact_root
        if configured_artifact_root is None:
            configured_artifact_root = self._env.get(
                "LITMUS_ARTIFACT_ROOT", Path.cwd() / "artifacts"
            )
        self._artifact_store = _ArtifactStore(configured_artifact_root)
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
        """Immutable snapshot of the run environment, as published in every ``RunResult``.

        This is the filtered view when the run declared ``env_allow`` or ``env_deny``;
        a variable the filter withheld is absent here too, so ``PHYSICAL_LOCATION`` and
        ``LITMUS_ARTIFACT_ROOT`` must survive an allow list to keep their effect.
        """
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
        and aborted commands settle as `exit_code == -1`. A closed transport
        (including after `cancel_in_flight`) settles the same way, with the
        protocol error on stderr — `RemoteInterface.exec` still raises
        `ConnectionError` internally; this method is the author-facing surface
        and stays total. `CancelledError` still propagates: that is TaskGroup
        teardown, not a command outcome.
        """
        self._logger.info("attempting to run command >%s<", command)
        try:
            result = await self._routing.exec(command)
        except ConnectionError as error:
            result = ExecResult.settled("", str(error), None)
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
        """Install a via device's route so this node's exec borrows that hop.

        A declared via with no hop is a framework bug, not a silent fall-through
        to local exec.
        """
        if router is None:
            node = self._scope[-1] if self._scope else "<unknown>"
            raise FrameworkContractViolation(
                node,
                detail="a via hop was declared but no exec route was supplied",
            )
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
        discrete_log_file: bool,
        isolate_context: bool,
        audit_signal: AuditSignal | None = None,
        inherit_route: bool = True,
    ) -> TestUtils:
        """Create one node-owned view from this node's child inheritance policy."""
        scoped = object.__new__(TestUtils)
        scoped._artifact_store = self._artifact_store
        scoped._ctx = {} if isolate_context else self._ctx
        scoped._env = self._env
        scoped._env_filter_warning = self._env_filter_warning
        scoped._base_logger = self._base_logger
        scoped._scope = (*self._scope, node)
        scoped._timeline = self._timeline
        scoped._owns_timeline = self._timeline is not None and (
            discrete_log_file or self._timeline_paths is None
        )
        if scoped._owns_timeline:
            if self._timeline is None:
                raise FrameworkContractViolation(
                    node,
                    detail="a node owns a timeline without an active run timeline",
                )
            scoped._timeline_paths = self._timeline.register(scoped._scope)
        else:
            scoped._timeline_paths = self._timeline_paths
        scoped._logger = ScopedLogger(
            self._base_logger,
            scoped._scope,
            timeline=scoped._timeline,
            timeline_paths=scoped._timeline_paths,
        )
        if not self._scope and scoped._env_filter_warning is not None:
            if scoped._timeline is not None and scoped._timeline_paths is not None:
                scoped._timeline.write(
                    scoped._timeline_paths,
                    scope=scoped._scope,
                    level=logging.WARNING,
                    message=scoped._env_filter_warning,
                    extra=None,
                )
            scoped._env_filter_warning = None
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

    async def _begin_run(self) -> None:
        """Open the run directory before the root lifecycle emits evidence."""
        run_root = await asyncio.to_thread(self._artifact_store.begin_run)
        self._timeline = RunTimeline(run_root)

    async def _end_run(self) -> None:
        """Close any timeline handle left by an exceptional lifecycle exit."""
        timeline = self._timeline
        if timeline is not None:
            await asyncio.to_thread(timeline.close_all)

    def _owned_log_path(self) -> Path | None:
        """Return this node's authoritative JSON timeline when it owns one."""
        if not self._owns_timeline or self._timeline_paths is None:
            return None
        return self._timeline_paths.json_lines

    def _owned_log_reference(self) -> str | None:
        """Return the summary-tree link to this node's timeline when it owns one.

        The link is the POSIX path of ``timeline.jsonl`` relative to the run directory,
        which is the form the Inspector resolves against the directory it opened.
        """
        if not self._owns_timeline or self._timeline is None or self._timeline_paths is None:
            return None
        return self._timeline_paths.json_lines.relative_to(self._timeline.root).as_posix()

    def _close_timeline(self) -> None:
        """Close this node's files at its terminal boundary."""
        if self._owns_timeline and self._timeline is not None and self._timeline_paths is not None:
            self._timeline.close(self._timeline_paths)
