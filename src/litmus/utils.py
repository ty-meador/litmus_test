"""The per-node utility surface handed to authored lifecycle code."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import re
import uuid
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, NoReturn, cast, final

from litmus.arp import ArpResolver
from litmus.artifacts import Artifact
from litmus.errors import (
    ContextImmutable,
    ContextInsufficient,
    ContractViolation,
    FrameworkContractViolation,
    HaltReason,
    TestFailure,
)
from litmus.executor import DEFAULT_EXEC_OUTPUT_BUDGET, ExecResult, LocalExec
from litmus.hooks import IntegrationHooks
from litmus.lifecycle import AuditSignal, RunAbort
from litmus.remote import RemoteInterface
from litmus.routing import (
    ExecRouter,
    ExecRouting,
    RouterRunParams,
    Shell,
    require_connection_pair,
)
from litmus.timeline import RunTimeline, TimelinePaths

if TYPE_CHECKING:
    from litmus.results import RunResult

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

    __slots__ = ("_close_lock", "_halted", "_late_closes", "_local_exec", "_remotes")

    def __init__(self, local_exec: LocalExec) -> None:
        self._local_exec = local_exec
        self._remotes: list[RemoteInterface] = []
        self._halted = False
        self._late_closes: set[asyncio.Task[None]] = set()
        self._close_lock = asyncio.Lock()

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
        async with self._close_lock:
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
        serialized = self._serialize(artifact.to_json())
        temporary = self._write_temporary(serialized, kind="artifact")
        try:
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

    async def persist_result(self, result: RunResult) -> Path:
        """Atomically replace this run's canonical Inspector result."""
        return await asyncio.to_thread(self._persist_result_sync, result)

    def _persist_result_sync(self, result: RunResult) -> Path:
        """Serialize and atomically publish the root result from an executor thread."""
        serialized = self._serialize(result.to_json())
        temporary = self._write_temporary(serialized, kind="result")
        destination = self._root / "result.json"
        try:
            temporary.replace(destination)
            return destination
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _serialize(payload: object) -> str:
        serialized = json.dumps(
            payload,
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        return f"{serialized}\n"

    def _write_temporary(self, serialized: str, *, kind: str) -> Path:
        """Write and fsync one private file for a later atomic publication."""
        self._root.mkdir(parents=True, exist_ok=True)
        temporary = self._root / f".{uuid.uuid4().hex}.{kind}.tmp"
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as output:
                output.write(serialized)
                output.flush()
                os.fsync(output.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return temporary


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
    def reason(self) -> HaltReason | TestFailure | None:
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
class RunContext(dict[str, Any]):
    """The ``ctx`` of one authored instance: what ``configure()`` provided, read-only after.

    ``configure()`` returns every key a test method, hook, or ``cleanup()`` reads by
    subscript. A ``[]`` read of anything else raises :class:`ContextInsufficient`, a
    contract violation, instead of the bare ``KeyError`` an author could catch by
    accident. ``get``, ``in``, and iteration are ordinary ``dict`` behaviour: they are
    the author declaring a key optional.

    The framework merges configure's return and then freezes the context. Every write
    after that point raises :class:`ContextImmutable`: a reading belongs on the verdict,
    and a value cleanup must restore is captured in ``configure()`` (issue #118). The
    ``run_data`` snapshot iterates it and ``json.dumps`` accepts it. ``owner`` is the
    authored instance whose ``configure()`` fills it, named once the instance binds.
    """

    __slots__ = ("frozen", "owner")

    def __init__(self, seed: Mapping[str, Any] | None = None, *, owner: str | None = None) -> None:
        """Start from a copy of ``seed``; ``owner`` may be named later by the framework."""
        super().__init__({} if seed is None else seed)
        self.owner = owner
        self.frozen = False

    def __missing__(self, key: str) -> NoReturn:
        raise ContextInsufficient(self.owner or "context", key=key, owner=self.owner)

    def _refuse(self, key: str) -> NoReturn:
        raise ContextImmutable(self.owner or "context", key=key, owner=self.owner)

    def _merge(self, provided: Mapping[str, Any]) -> None:
        """Merge configure's return. Framework only; the context is still open."""
        for key, value in provided.items():
            super().__setitem__(key, value)

    def _freeze(self) -> None:
        """Close the context once ``configure()`` has settled. Framework only."""
        self.frozen = True

    def __setitem__(self, key: str, value: Any) -> None:
        if self.frozen:
            self._refuse(key)
        super().__setitem__(key, value)

    def __delitem__(self, key: str) -> None:
        if self.frozen:
            self._refuse(key)
        super().__delitem__(key)

    def update(self, *args: Any, **kwargs: Any) -> None:  # pyright: ignore[reportIncompatibleMethodOverride]
        if self.frozen:
            self._refuse(_first_key(args, kwargs, "update(...)"))
        super().update(*args, **kwargs)  # pyright: ignore[reportUnknownMemberType]

    def setdefault(self, key: str, default: Any = None) -> Any:
        if self.frozen and key not in self:
            self._refuse(key)
        return super().setdefault(key, default)

    def pop(self, key: str, *default: Any) -> Any:
        if self.frozen:
            self._refuse(key)
        return super().pop(key, *default)

    def popitem(self) -> tuple[str, Any]:
        if self.frozen:
            self._refuse("popitem()")
        return super().popitem()

    def clear(self) -> None:
        if self.frozen:
            self._refuse("clear()")
        super().clear()

    def __ior__(self, other: Any) -> RunContext:  # pyright: ignore[reportIncompatibleMethodOverride]
        if self.frozen:
            self._refuse(_first_key((other,), {}, "|="))
        super().__ior__(other)
        return self


def _first_key(args: tuple[Any, ...], kwargs: Mapping[str, Any], operation: str) -> str:
    """Name the first key a bulk write would have touched, for the violation message."""
    for arg in args:
        keys = list(arg.keys()) if isinstance(arg, Mapping) else [pair[0] for pair in arg]  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType, reportUnknownMemberType]
        if keys:
            return str(keys[0])  # pyright: ignore[reportUnknownArgumentType]
    for key in kwargs:
        return key
    return operation


@final
class RunEnv(Mapping[str, str]):
    """The run environment: a read-only mapping with readers that Fail the run.

    Contract:
      - It is the filtered, immutable snapshot ``TestUtils.env`` publishes on every
        ``RunResult``. ``get``, ``in``, ``[]``, and iteration behave as on any mapping.
      - ``text(name)`` trims the value and treats blank as unset. A bench script that
        exports ``TARGET_PASSWORD=""`` means "no password", not "the empty password".
      - ``require(name)`` raises ``TestFailure`` naming the variable to set. Called from
        ``configure``, that is the "did not reach a testable state" verdict: the run
        Fails before any claim runs, and the message is the operator's instruction.
      - ``port(name, default=22)`` parses an integer in 1-65535 and raises
        ``TestFailure`` for anything else, so a bench typo is reported as the variable
        rather than surfacing later as a transport error.
    """

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, str]) -> None:
        self._values: Mapping[str, str] = MappingProxyType(dict(values))

    def __getitem__(self, name: str) -> str:
        return self._values[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return f"RunEnv({dict(self._values)!r})"

    def text(self, name: str) -> str | None:
        """Return the trimmed value of ``name``, or ``None`` when it is unset or blank."""
        value = self._values.get(name)
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    def require(self, name: str) -> str:
        """Return the trimmed value of ``name``, or Fail the run naming the variable.

        Raises:
            TestFailure: ``name`` is unset or blank.
        """
        value = self.text(name)
        if value is None:
            raise TestFailure(f"did not reach a testable state: {name} env var not set; set {name}")
        return value

    def port(self, name: str, *, default: int = 22) -> int:
        """Return ``name`` as a TCP port, or ``default`` when it is unset or blank.

        Raises:
            TestFailure: the value is not an integer in 1-65535.
        """
        raw = self.text(name)
        if raw is None:
            return default
        try:
            port = int(raw)
        except ValueError:
            port = 0
        if not 1 <= port <= 65535:
            raise TestFailure(
                f"did not reach a testable state: {name} env var is not a valid port; "
                f"set {name} to an integer 1-65535"
            )
        return port


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
        "_in_configure",
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
        output_budget: int = DEFAULT_EXEC_OUTPUT_BUDGET,
        _integration_hooks: IntegrationHooks | None = None,
    ) -> None:
        """Create an unscoped run seed; lifecycle execution creates node views.

        Raises ``ValueError`` when both ``env_allow`` and ``env_deny`` are given.

        Args:
            ctx: Initial root context, copied into the run's context; read it back
                through :attr:`ctx`. The framework mutates that copy in place.
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
            output_budget: Combined stdout+stderr ceiling in bytes for every
                ``exec`` this run issues, local or SSH. Defaults to
                ``DEFAULT_EXEC_OUTPUT_BUDGET`` (4 MiB).
        """
        self._ctx = RunContext(ctx)
        self._base_logger = LOGGER if logger is None else logger
        self._scope: tuple[str, ...] = ()
        self._logger = ScopedLogger(self._base_logger, self._scope)
        self._timeline: RunTimeline | None = None
        self._timeline_paths: TimelinePaths | None = None
        self._owns_timeline = False
        source_env = os.environ if env is None else env
        env_filter = _EnvFilter(allow=env_allow, deny=env_deny)
        self._env = RunEnv(env_filter.apply(source_env, self._base_logger))
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
        self._local_exec = LocalExec(output_budget=output_budget)
        self._transports = _RunTransports(self._local_exec)
        self._integration_hooks = (
            IntegrationHooks() if _integration_hooks is None else _integration_hooks
        )
        self._routing = ExecRouting(
            self._local_exec.run,
            logger=self._logger,
            on_open=self._transports.register,
            output_budget=self._local_exec.output_budget,
        )
        self._signal = RunSignal(RunAbort())
        self._in_configure = False

    @property
    def logger(self) -> ScopedLogger:
        """Logger whose records are tagged with this node's full scope."""
        return self._logger

    @property
    def ctx(self) -> Mapping[str, Any]:
        """This authored instance's context: what ``configure()`` provided, read-only after."""
        return self._ctx

    @property
    def env(self) -> RunEnv:
        """Immutable snapshot of the run environment, as published in every ``RunResult``.

        This is the filtered view when the run declared ``env_allow`` or ``env_deny``;
        a variable the filter withheld is absent here too, so ``PHYSICAL_LOCATION`` and
        ``LITMUS_ARTIFACT_ROOT`` must survive an allow list to keep their effect. Beyond
        the mapping, ``RunEnv.require``, ``text``, and ``port`` read a bench variable
        and Fail the run naming it when it is missing or malformed.
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

    @property
    def exec_route(self) -> str:
        """The host ``utils.exec`` lands on right now: ``"local"`` or the hop's endpoint.

        ``"local"`` is the test host, before any ``use_shell`` and after
        ``use_shell(inherit=False)``. Otherwise it is the endpoint of the hop this
        node's route attaches to, the last name in the ``exec routing:`` evidence line
        (``"10.10.10.10"`` for a route ``bastion -> 10.10.10.10``). A survey reads this
        to know whether there is a device on the other end of ``exec`` at the phase it
        fires, which the phase alone cannot say.
        """
        router = self._routing.router
        if router is None or router.ssh is None:
            return "local"
        return router.ssh.endpoint

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

    async def _persist_result(self, result: RunResult) -> Path:
        """Publish the root result before appending its final timeline pointer."""
        result_path = await self._artifact_store.persist_result(result)
        timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        self._logger.info(
            "run result persisted -> %s",
            result_path,
            extra={
                "artifact_name": "result",
                "artifact_path": str(result_path),
                "artifact_timestamp": timestamp,
                "artifact_type": "run-result",
            },
        )
        return result_path

    async def exec(self, command: str) -> ExecResult:
        """Run ``command`` through this node's active route.

        Never raises for the command's own exit status. A non-zero `exit_code`
        is evidence for the test method to judge, not an exception. Signal-killed
        and aborted commands settle as `exit_code == -1`. A closed transport
        (including after `cancel_in_flight`) settles the same way, with the
        protocol error on stderr — `RemoteInterface.exec` still raises
        `ConnectionError` internally; this method is the author-facing surface
        and stays total. Hitting the output budget terminates the command and
        settles with `truncated=True`; that is evidence, not an exception.
        `CancelledError` still propagates: that is TaskGroup teardown, not a
        command outcome.
        """
        self._logger.info("attempting to run command >%s<", command)
        try:
            result = await self._routing.exec(command)
        except ConnectionError as error:
            result = ExecResult.settled("", str(error), None)
        if result.stderr:
            self._logger.error("stderr: \n%s", result.stderr)
        self._logger.info("exit code: %s", result.exit_code)
        if result.truncated:
            self._logger.warning(
                "exec output truncated: captured %s bytes (budget %s)",
                result.captured_bytes,
                self._local_exec.output_budget,
            )
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
            raise ValueError(
                "wait_for interval must be greater than zero; pass a positive number of seconds"
            )
        if timeout_seconds < 0:
            raise ValueError(
                "wait_for timeout must not be negative; pass zero or a positive number of seconds"
            )

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

    def get_arp_resolver(self, *, mac: str, ping_count: int = 1) -> ArpResolver:
        """Return a MAC-to-IP resolver scoped to this node's current exec route.

        ARP table reads and pings go through `utils.exec`, so they land on the
        same host as the rest of this node's commands — local, or through a via
        hop. The routing label is the cache key: devices behind different
        gateways keep independent ARP state.

        Args:
            mac: Device MAC in any usual form (colons, dashes, or bare hex).
            ping_count: ICMP packets per `ping()`. Defaults to 1.
        """
        router = self._routing.router
        cache_key = router.label if router is not None else "local"
        return ArpResolver(
            mac=mac,
            ping_count=ping_count,
            exec_fn=self.exec,
            logger=self._logger,
            cache_key=cache_key,
        )

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
        """Declare this node's exec route from ``configure()``, or read the active one.

        With no arguments this returns the active ``Shell`` and may be called from any
        slot. A route-shaping argument (``host``, ``via``, ``inherit=False``,
        ``propagate=False``) declares the route, and the framework admits that only
        while this node's ``configure()`` is running: the ``exec routing:`` line is
        written when configure returns and the FT reads it as true for the whole node
        (Exec Routing Contract). From any other slot the declaring call raises
        ``ContractViolation`` before the router is touched.
        """
        require_connection_pair(host, username)
        declares = host is not None or via is not None or not inherit or not propagate
        if declares and not self._in_configure:
            node = self._scope[-1] if self._scope else "<unknown>"
            raise ContractViolation(
                node,
                detail=(
                    "use_shell declared a route outside configure(); the exec route is "
                    "fixed when configure() returns so the 'exec routing:' line stays "
                    "true for the whole node; declare it there, or call use_shell() "
                    "with no arguments to read the active route"
                ),
            )
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

    async def _cancel_opened(self) -> None:
        """Hard-close this node's remotes without touching an inherited hop."""
        await self._routing.cancel_opened()

    def _own_context(self, node: str) -> None:
        """Name the authored instance whose ``configure()`` fills this context. Framework only."""
        if self._ctx.owner is None:
            self._ctx.owner = node

    def _merge_context(self, provided: Mapping[str, Any]) -> None:
        """Merge ``configure()``'s return into the still-open context. Framework only."""
        self._ctx._merge(provided)  # pyright: ignore[reportPrivateUsage]

    def _freeze_context(self) -> None:
        """Close the context once ``configure()`` has settled. Framework only."""
        self._ctx._freeze()  # pyright: ignore[reportPrivateUsage]

    def _enter_configure(self) -> None:
        """Open the route-declaration window. Framework only, around ``configure()``."""
        self._in_configure = True

    def _exit_configure(self) -> None:
        """Shut the window once ``configure()`` has settled, returned or raised."""
        self._in_configure = False

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
        scoped._ctx = RunContext(owner=node) if isolate_context else self._ctx
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
            output_budget=self._local_exec.output_budget,
        )
        scoped._signal = RunSignal(run_abort, audit_signal)
        scoped._in_configure = False
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
