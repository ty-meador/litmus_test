"""The per-node utility surface handed to authored lifecycle code."""

from __future__ import annotations

import asyncio
import contextlib
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
from litmus.device_api import CoapInterface, DeviceApi, DeviceApiInterface, HttpInterface
from litmus.errors import (
    ContextImmutable,
    ContextInsufficient,
    ContractViolation,
    FrameworkContractViolation,
    HaltReason,
    MeasurementDuplicate,
    MeasurementSealed,
    TestFailure,
)
from litmus.executor import DEFAULT_EXEC_OUTPUT_BUDGET, ExecResult, LocalExec
from litmus.hooks import IntegrationHooks
from litmus.lifecycle import AuditSignal, RunAbort
from litmus.measurement import (
    Limits,
    Measurement,
    current_utils,
    measurement_label,
    new_measurement,
)
from litmus.remote import RemoteInterface
from litmus.routing import (
    ExecRouting,
    Shell,
    require_connection_pair,
)
from litmus.timeline import RunTimeline, TimelinePaths

if TYPE_CHECKING:
    from litmus.results import RunResult

type WaitPredicate = Callable[[], bool | Awaitable[bool]]

LOGGER = logging.getLogger(__name__)


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
    """Every transport and device-API session opened during one run."""

    __slots__ = (
        "_close_lock",
        "_device_apis",
        "_halted",
        "_late_closes",
        "_local_exec",
        "_remotes",
    )

    def __init__(self, local_exec: LocalExec) -> None:
        self._local_exec = local_exec
        self._remotes: list[RemoteInterface] = []
        self._device_apis: list[DeviceApiInterface] = []
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

    def register_device_api(self, handle: DeviceApiInterface) -> None:
        """Remember a device-API session for the run-level halt sweep."""
        if all(known is not handle for known in self._device_apis):
            self._device_apis.append(handle)
        if self._halted and handle.connected:
            close = asyncio.create_task(self._close_device_api(handle))
            self._late_closes.add(close)
            close.add_done_callback(self._late_closes.discard)

    async def cancel_in_flight(self) -> None:
        """Close local work, remotes, and device-API sessions together."""
        async with self._close_lock:
            self._halted = True
            await asyncio.gather(
                self._close_local(),
                *(self._close_remote(remote) for remote in tuple(self._remotes)),
                *(self._close_device_api(handle) for handle in tuple(self._device_apis)),
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

    async def _close_device_api(self, handle: DeviceApiInterface) -> None:
        try:
            await handle.cancel_in_flight()
        except Exception:
            LOGGER.exception("run-abort could not terminate device-api %s", handle.handle_label)


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


def _measurement_line(measurement: Measurement) -> str:
    """Render the timeline line for one reading in the shape ``docs/measurements.md`` fixes.

    JSON vocabulary (``null``, ``true``, ``false``) rather than Python's, so the log
    line and the record read the same way to someone comparing them.
    """
    text = f"{measurement.name} = {measurement.value}"
    if measurement.unit is not None:
        text = f"{text} {measurement.unit}"
    if measurement.limits is None:
        return f"{text} limits=null in_limits=null"
    low, high = measurement.limits
    bounds = ", ".join("null" if bound is None else str(bound) for bound in (low, high))
    verdict = "true" if measurement.in_limits else "false"
    return f"{text} limits=[{bounds}] in_limits={verdict}"


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
        """Merge while the context is open; refused once ``configure()`` has settled."""
        if self.frozen:
            self._refuse(_first_key(args, kwargs, "update(...)"))
        super().update(*args, **kwargs)  # pyright: ignore[reportUnknownMemberType]

    def setdefault(self, key: str, default: Any = None) -> Any:
        """Read an existing key freely; inserting a new one is refused once frozen."""
        if self.frozen and key not in self:
            self._refuse(key)
        return super().setdefault(key, default)

    def pop(self, key: str, *default: Any) -> Any:
        """Remove while open; refused once frozen, since ctx is read-only after ``configure()``."""
        if self.frozen:
            self._refuse(key)
        return super().pop(key, *default)

    def popitem(self) -> tuple[str, Any]:
        """Refused once frozen: a frozen context loses no keys."""
        if self.frozen:
            self._refuse("popitem()")
        return super().popitem()

    def clear(self) -> None:
        """Refused once frozen: a frozen context loses no keys."""
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
class NodeRuntime:
    """The lifecycle-facing half of a node's service object. Package-internal.

    A node's ``TestUtils`` is two things: the surface authored code reads (``exec``,
    ``ctx``, ``measure``, ...) and the state the lifecycle in ``base.py`` drives
    around that code (the run bracket, the context phases, transport teardown,
    timeline ownership, measurement sealing, router inheritance). This class is the
    second thing. ``base.py`` holds one per node and drives it by these plain names;
    :class:`TestUtils` holds the same object and reads its state on the author's
    behalf, so the type checker sees every lifecycle call instead of a suppressed
    private one (issue #126). Nothing here is in ``litmus.__all__``: a suite never
    sees a runtime, and :func:`node_runtime` is how the framework reaches it.

    The state is plain attributes, not properties: the runtime is a record the two
    halves share, and a property on each field would say nothing the name does not.
    """

    __slots__ = (
        "artifact_store",
        "base_logger",
        "ctx",
        "env",
        "env_filter_warning",
        "in_configure",
        "integration_hooks",
        "local_exec",
        "logger",
        "measurements",
        "opened_device_apis",
        "owns_timeline",
        "routing",
        "scope",
        "signal",
        "suite_target",
        "timeline",
        "timeline_paths",
        "transports",
        "utils",
    )

    def __init__(
        self,
        utils: TestUtils[Any],
        *,
        ctx: dict[str, Any] | None,
        env: Mapping[str, str] | None,
        env_allow: Iterable[str] | None,
        env_deny: Iterable[str] | None,
        logger: logging.Logger | None,
        artifact_root: str | Path | None,
        output_budget: int,
        integration_hooks: IntegrationHooks | None,
        suite_target: str | None,
    ) -> None:
        """Build the unscoped run seed behind ``utils``; :meth:`for_node` scopes it."""
        self.utils = utils
        self.suite_target = suite_target
        self.ctx = RunContext(ctx)
        self.base_logger = LOGGER if logger is None else logger
        self.scope: tuple[str, ...] = ()
        self.logger = ScopedLogger(self.base_logger, self.scope)
        self.timeline: RunTimeline | None = None
        self.timeline_paths: TimelinePaths | None = None
        self.owns_timeline = False
        source_env = os.environ if env is None else env
        env_filter = _EnvFilter(allow=env_allow, deny=env_deny)
        self.env = RunEnv(env_filter.apply(source_env, self.base_logger))
        self.env_filter_warning: str | None = None
        if (
            env_filter.mode is not None
            and source_env.get(_EnvFilter.DEBUG_KEY) == _EnvFilter.DEBUG_VALUE
        ):
            self.env_filter_warning = (
                f"DEBUG mode detected, ignoring provided {env_filter.mode} list "
                "and including ALL environment variables"
            )
        configured_artifact_root = artifact_root
        if configured_artifact_root is None:
            configured_artifact_root = self.env.get(
                "LITMUS_ARTIFACT_ROOT", Path.cwd() / "artifacts"
            )
        self.artifact_store = _ArtifactStore(configured_artifact_root)
        self.local_exec = LocalExec(output_budget=output_budget)
        self.transports = _RunTransports(self.local_exec)
        self.integration_hooks = (
            IntegrationHooks() if integration_hooks is None else integration_hooks
        )
        self.routing = ExecRouting(
            self.local_exec.run,
            logger=self.logger,
            on_open=self.transports.register,
            output_budget=self.local_exec.output_budget,
        )
        self.signal = RunSignal(RunAbort())
        self.in_configure = False
        self.opened_device_apis: list[DeviceApiInterface] = []
        # A list while the node's slots may still record; a tuple once sealed.
        self.measurements: list[Measurement] | tuple[Measurement, ...] = []

    # --- the run bracket ---------------------------------------------------------

    async def begin_run(self) -> None:
        """Open the run directory before the root lifecycle emits evidence."""
        run_root = await asyncio.to_thread(self.artifact_store.begin_run)
        self.timeline = RunTimeline(run_root)

    async def end_run(self) -> None:
        """Close any timeline handle left by an exceptional lifecycle exit."""
        timeline = self.timeline
        if timeline is not None:
            await asyncio.to_thread(timeline.close_all)

    async def persist_result(self, result: RunResult) -> Path:
        """Publish the root result before appending its final timeline pointer."""
        result_path = await self.artifact_store.persist_result(result)
        timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        self.logger.info(
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

    # --- one node's view ----------------------------------------------------------

    def for_node(
        self,
        node: str,
        run_abort: RunAbort,
        *,
        discrete_log_file: bool,
        isolate_context: bool,
        audit_signal: AuditSignal | None = None,
    ) -> NodeRuntime:
        """Create one node-owned runtime, and its ``TestUtils`` view, from this node's policy."""
        scoped: NodeRuntime = object.__new__(NodeRuntime)
        scoped.utils = TestUtils(_runtime=scoped)
        scoped.suite_target = self.suite_target
        scoped.artifact_store = self.artifact_store
        scoped.ctx = RunContext(owner=node) if isolate_context else self.ctx
        scoped.env = self.env
        scoped.env_filter_warning = self.env_filter_warning
        scoped.base_logger = self.base_logger
        scoped.scope = (*self.scope, node)
        scoped.timeline = self.timeline
        scoped.owns_timeline = self.timeline is not None and (
            discrete_log_file or self.timeline_paths is None
        )
        if scoped.owns_timeline:
            if self.timeline is None:
                raise FrameworkContractViolation(
                    node,
                    detail="a node owns a timeline without an active run timeline",
                )
            scoped.timeline_paths = self.timeline.register(scoped.scope)
        else:
            scoped.timeline_paths = self.timeline_paths
        scoped.logger = ScopedLogger(
            self.base_logger,
            scoped.scope,
            timeline=scoped.timeline,
            timeline_paths=scoped.timeline_paths,
        )
        if not self.scope and scoped.env_filter_warning is not None:
            if scoped.timeline is not None and scoped.timeline_paths is not None:
                scoped.timeline.write(
                    scoped.timeline_paths,
                    scope=scoped.scope,
                    level=logging.WARNING,
                    message=scoped.env_filter_warning,
                    extra=None,
                )
            scoped.env_filter_warning = None
        scoped.local_exec = self.local_exec
        scoped.transports = self.transports
        scoped.integration_hooks = self.integration_hooks
        scoped.routing = ExecRouting(
            self.local_exec.run,
            inherited=self.routing.child_run_params().router,
            logger=scoped.logger,
            on_open=self.transports.register,
            output_budget=self.local_exec.output_budget,
        )
        scoped.signal = RunSignal(run_abort, audit_signal)
        scoped.in_configure = False
        scoped.opened_device_apis = []
        scoped.measurements = []
        return scoped

    def halt_signal(self) -> RunAbort:
        """The run abort this node was scoped with."""
        return self.signal._run_abort  # pyright: ignore[reportPrivateUsage]

    # --- the context phases -------------------------------------------------------

    def own_context(self, node: str) -> None:
        """Name the authored instance whose ``configure()`` fills this context."""
        if self.ctx.owner is None:
            self.ctx.owner = node

    def enter_configure(self) -> None:
        """Open the route-declaration window around ``configure()``."""
        self.in_configure = True

    def merge_context(self, provided: Mapping[str, Any]) -> None:
        """Merge ``configure()``'s return into the still-open context."""
        self.ctx._merge(provided)  # pyright: ignore[reportPrivateUsage]

    def exit_configure(self) -> None:
        """Shut the window once ``configure()`` has settled, returned or raised."""
        self.in_configure = False

    def freeze_context(self) -> None:
        """Close the context once ``configure()`` has settled."""
        self.ctx._freeze()  # pyright: ignore[reportPrivateUsage]

    # --- routing and transport teardown ----------------------------------------------

    def log_exec_routing(self) -> None:
        """Record this node's topology line, once per executed node.

        Placed after ``configure`` for an authored node, since ``use_shell`` lives there
        and the route is not final until it returns.
        """
        self.logger.info("exec routing: %s", self.routing.routing_line())

    async def disconnect_opened(self) -> None:
        """Close remotes and device-API sessions this node opened."""
        await self.routing.disconnect_opened()
        opened = list(reversed(self.opened_device_apis))
        self.opened_device_apis.clear()
        for handle in opened:
            confirmed = False
            with contextlib.suppress(Exception):
                await handle.disconnect()
                confirmed = handle.close_confirmed
            self.logger.info(
                "device-api close: %s confirmed=%s",
                handle.handle_label,
                confirmed,
            )

    async def cancel_opened(self) -> None:
        """Hard-close this node's remotes and device-API sessions."""
        await self.routing.cancel_opened()
        opened = list(reversed(self.opened_device_apis))
        self.opened_device_apis.clear()
        await asyncio.gather(*(self._cancel_one_device_api(handle) for handle in opened))

    async def cancel_in_flight(self) -> None:
        """Hard-close every framework-managed transport known to this run."""
        await self.transports.cancel_in_flight()

    async def _cancel_one_device_api(self, handle: DeviceApiInterface) -> None:
        confirmed = False
        with contextlib.suppress(Exception):
            await handle.cancel_in_flight()
            confirmed = handle.close_confirmed
        self.logger.info(
            "device-api close: %s confirmed=%s",
            handle.handle_label,
            confirmed,
        )

    # --- the node's record ------------------------------------------------------

    def seal_measurements(self) -> tuple[Measurement, ...]:
        """Close this node's measurement record and return it.

        Called when the node settles, on every terminal state; calling it again
        returns the same record. Sealing is what makes a late ``measure`` a
        ``ContractViolation``, in the pattern of ``ContextImmutable``.
        """
        if not isinstance(self.measurements, tuple):
            self.measurements = tuple(self.measurements)
        return self.measurements

    def owned_log_path(self) -> Path | None:
        """Return this node's authoritative JSON timeline when it owns one."""
        if not self.owns_timeline or self.timeline_paths is None:
            return None
        return self.timeline_paths.json_lines

    def owned_log_reference(self) -> str | None:
        """Return the summary-tree link to this node's timeline when it owns one.

        The link is the POSIX path of ``timeline.jsonl`` relative to the run directory,
        which is the form the Inspector resolves against the directory it opened.
        """
        if not self.owns_timeline or self.timeline is None or self.timeline_paths is None:
            return None
        return self.timeline_paths.json_lines.relative_to(self.timeline.root).as_posix()

    def close_timeline(self) -> None:
        """Close this node's files at its terminal boundary."""
        if self.owns_timeline and self.timeline is not None and self.timeline_paths is not None:
            self.timeline.close(self.timeline_paths)


def node_runtime(utils: TestUtils[Any]) -> NodeRuntime:
    """The runtime behind a ``TestUtils``; the framework's way in, never the suite's.

    A plain function rather than a property so the surface a suite author reads on
    ``utils`` stays what the reference table lists.
    """
    # The one door between the two halves; every other lifecycle call goes through
    # the runtime it returns. Kept private on TestUtils so no suite reads it by accident.
    return utils._runtime  # pyright: ignore[reportPrivateUsage]


class TestUtils[CtxT: Mapping[str, Any] = Mapping[str, Any]]:
    """The single fully scoped service object passed to every lifecycle method.

    A node owns the object, logger, and routing policy. Its authored composition
    receives distinct ``TestUtils`` objects which deliberately share that authored
    instance's mutable ``ctx``. A composed ``LitmusTest`` subclass instead begins with
    a fresh context, preserving instance locality.

    ``CtxT`` is opt-in sugar (issue #75): the runtime object is always a
    :class:`RunContext`, a framework-owned ``dict[str, Any]``. A suite that narrows
    ``configure``'s return to a local ``TypedDict`` and declares
    ``class MySuite(LitmusTest[MyCtx])`` gets that type back from :attr:`ctx` in every
    method it types as ``utils: TestUtils[MyCtx]`` — pyright then catches a typo in a
    ctx key at the call site. Nothing forces a suite to parameterize; the default
    ``Mapping[str, Any]`` reproduces today's un-parameterized behavior exactly.

    Everything the lifecycle drives lives on the :class:`NodeRuntime` this object holds;
    the methods here are the author's reads and writes of that same state.
    """

    __test__ = False
    __slots__ = ("_runtime",)

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
        _runtime: NodeRuntime | None = None,
        _suite_target: str | None = None,
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
        if _runtime is not None:
            # A node view over a runtime the lifecycle already scoped (NodeRuntime.for_node).
            self._runtime = _runtime
            return
        self._runtime = NodeRuntime(
            self,
            ctx=ctx,
            env=env,
            env_allow=env_allow,
            env_deny=env_deny,
            logger=logger,
            artifact_root=artifact_root,
            output_budget=output_budget,
            integration_hooks=_integration_hooks,
            suite_target=_suite_target,
        )

    @property
    def logger(self) -> ScopedLogger:
        """Logger whose records are tagged with this node's full scope."""
        return self._runtime.logger

    @property
    def ctx(self) -> CtxT:
        """This authored instance's context: what ``configure()`` provided, read-only after.

        Always a :class:`RunContext` at runtime regardless of ``CtxT``; the cast here is
        the framework's, narrowing to the suite's declared shape for an author who typed
        ``utils: TestUtils[MyCtx]`` (issue #75). An un-parameterized caller sees the
        same ``Mapping[str, Any]`` as before.
        """
        return cast(CtxT, self._runtime.ctx)

    @property
    def env(self) -> RunEnv:
        """Immutable snapshot of the run environment, as published in every ``RunResult``.

        This is the filtered view when the run declared ``env_allow`` or ``env_deny``;
        a variable the filter withheld is absent here too, so ``PHYSICAL_LOCATION`` and
        ``LITMUS_ARTIFACT_ROOT`` must survive an allow list to keep their effect. Beyond
        the mapping, ``RunEnv.require``, ``text``, and ``port`` read a bench variable
        and Fail the run naming it when it is missing or malformed.
        """
        return self._runtime.env

    @property
    def signal(self) -> RunSignal:
        """Read-only view of the run-level integration halt signal."""
        return self._runtime.signal

    @property
    def artifact_root(self) -> Path:
        """Absolute directory where this run's captured evidence is stored."""
        return self._runtime.artifact_store.root

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
        router = self._runtime.routing.router
        if router is None or router.ssh is None:
            return "local"
        return router.ssh.endpoint

    async def capture_artifact(self, artifact: Artifact) -> Path:
        """Persist evidence and append its structured pointer to the scoped timeline.

        The file is durably published before the pointer is logged. Serialization or
        persistence failures therefore produce neither a misleading pointer nor a
        partially published artifact.
        """
        artifact_path = await self._runtime.artifact_store.capture(artifact)
        timestamp = artifact.timestamp.isoformat().replace("+00:00", "Z")
        self._runtime.logger.info(
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
        and stays total. Hitting the output budget terminates the command and
        settles with `truncated=True`; that is evidence, not an exception.
        `CancelledError` still propagates: that is TaskGroup teardown, not a
        command outcome.
        """
        logger = self._runtime.logger
        logger.info("attempting to run command >%s<", command)
        try:
            result = await self._runtime.routing.exec(command)
        except ConnectionError as error:
            result = ExecResult.settled("", str(error), None)
        if result.stderr:
            logger.error("stderr: \n%s", result.stderr)
        logger.info("exit code: %s", result.exit_code)
        if result.truncated:
            logger.warning(
                "exec output truncated: captured %s bytes (budget %s)",
                result.captured_bytes,
                self._runtime.local_exec.output_budget,
            )
        logger.info("stdout: \n%s", result.stdout)
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
        router = self._runtime.routing.router
        cache_key = router.label if router is not None else "local"
        return ArpResolver(
            mac=mac,
            ping_count=ping_count,
            exec_fn=self.exec,
            logger=self._runtime.logger,
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
        if declares and not self._runtime.in_configure:
            scope = self._runtime.scope
            node = scope[-1] if scope else "<unknown>"
            raise ContractViolation(
                node,
                detail=(
                    "use_shell declared a route outside configure(); the exec route is "
                    "fixed when configure() returns so the 'exec routing:' line stays "
                    "true for the whole node; declare it there, or call use_shell() "
                    "with no arguments to read the active route"
                ),
            )
        return await self._runtime.routing.use_shell(
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
        return await self._runtime.routing.get_shell(
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

    async def get_http(
        self,
        *,
        base_url: str,
        headers: Mapping[str, str] | None = None,
        token: str | None = None,
        verify: bool = True,
        follow_redirects: bool = False,
        request_timeout: float = 20.0,
    ) -> DeviceApi:
        """Open an HTTP session. Does not change this node's exec route.

        Status on the response is data, including 4xx/5xx. Credentials in
        ``token`` never reach evidence. Abort closes the session; it does not
        invent a shutdown request.

        Args:
            base_url: Origin, including ``http`` or ``https``.
            headers: Default headers for every request on this handle.
            token: Optional Bearer token. Named as a path in logs, never the value.
            verify: TLS verification. ``False`` is lab-only and logged as such.
            follow_redirects: Same-origin follow. Cross-host Location is data.
            request_timeout: Positive finite seconds per attempt.
        """
        merged = dict(headers) if headers is not None else {}
        if token is not None:
            merged["Authorization"] = f"Bearer {token}"
        backend = HttpInterface(
            base_url=base_url,
            headers=merged,
            verify=verify,
            follow_redirects=follow_redirects,
            request_timeout=request_timeout,
            logger=self._runtime.logger,
        )
        return await self._open_device_api(backend)

    async def get_coap(
        self,
        *,
        base_url: str,
        headers: Mapping[str, str] | None = None,
        request_timeout: float = 20.0,
    ) -> DeviceApi:
        """Open a CoAP session. Does not change this node's exec route.

        Scheme identity is ``coap`` (UDP CON/ACK). ``coaps`` / ``coaps+tcp``
        are not this backend. Status is the CoAP code; 4.xx is data.

        Args:
            base_url: Origin, including the ``coap`` scheme.
            headers: Reserved for CoAP options on a later backend; unused here.
            request_timeout: Positive finite seconds per attempt.
        """
        backend = CoapInterface(
            base_url=base_url,
            headers=headers,
            request_timeout=request_timeout,
            logger=self._runtime.logger,
        )
        return await self._open_device_api(backend)

    def measure(
        self,
        name: str,
        value: int | float,
        *,
        unit: str | None = None,
        limits: Limits | None = None,
    ) -> Measurement:
        """Record one parametric reading on this node, beside the verdict.

        A limit is evidence, never a verdict: the reading is recorded with
        ``in_limits`` decided and nothing about the node's state changes. When the
        window is a pass criterion, gate on the returned record in plain sight so the
        verdict's predicate is visible to the reader and to ``litmus/gated-pass``::

            rail = utils.measure("output_voltage_v", volts, unit="V", limits=(4.75, 5.25))
            if rail.in_limits:
                return PASS({"output_voltage_v": volts})
            raise TestFailure("rail outside window", "4.75-5.25 V", f"{volts} V")

        Synchronous and free of I/O, so no cancellation can land between the reading
        and its record. Allowed from ``configure()`` and the test method only, the two
        slots that produce the node's record; the record is sealed when the node
        settles. ``litmus.measure`` is the same call resolved to the running node.

        Args:
            name: Non-empty identity of the quantity, unique within this node. For the
                life of the suite it names the same quantity in the same unit; a
                consumer keys its series on it.
            value: An ``int`` or finite ``float``. A textual observation is evidence on
                the verdict, not a measurement.
            unit: Free label such as ``"V"`` or ``"ms"``, or ``None`` for a
                dimensionless count. Never validated, never converted.
            limits: Inclusive ``(low, high)`` window with ``None`` on an open side, or
                ``None`` to record for trend with ``in_limits`` unset.

        Returns:
            The frozen ``Measurement`` exactly as recorded.

        Raises:
            ContractViolation: When an argument is refused, when the name was already
                recorded in this node, or when the call comes from outside
                ``configure()`` or the test method.
        """
        runtime = self._runtime
        node = runtime.scope[-1] if runtime.scope else "<unscoped>"
        # The task-local binding, not a flag on this object, is the slot guard: an
        # audit sampler shares this object but runs in a task that was created before
        # the slot bound it, so it sees no binding and is refused.
        if current_utils.get() is not self or isinstance(runtime.measurements, tuple):
            raise MeasurementSealed(node, measurement=measurement_label(name))
        measurement = new_measurement(node, name, value, unit=unit, limits=limits)
        if any(recorded.name == measurement.name for recorded in runtime.measurements):
            raise MeasurementDuplicate(node, measurement=measurement.name)
        runtime.measurements.append(measurement)
        # One scoped line per call, so the chronology carries the number next to what
        # the suite was doing; a warning when out of window so it stands out in
        # timeline.log without the Floor Technician filtering for it.
        level = logging.WARNING if measurement.in_limits is False else logging.INFO
        runtime.logger.log(level, "measurement: %s", _measurement_line(measurement))
        return measurement

    async def _open_device_api(self, backend: DeviceApiInterface) -> DeviceApi:
        """Connect, register for halt, and log the session without credentials."""
        runtime = self._runtime
        runtime.logger.info("device-api connect: %s", backend.handle_label)
        await backend.connect()
        runtime.opened_device_apis.append(backend)
        runtime.transports.register_device_api(backend)
        return DeviceApi(backend)
